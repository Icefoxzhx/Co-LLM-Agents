import random
import re
from typing import List

import openai
import json
import os
import pandas as pd
from openai.error import OpenAIError
import backoff
import torch
from tqdm import tqdm
from transformers import AutoTokenizer, AutoModelForCausalLM, LlamaForCausalLM, LlamaTokenizer

class LLM:
	def __init__(self,
				 source,  # 'huggingface' or 'openai'
				 lm_id,
				 prompt_template_path,
				 communication,
				 cot,
				 sampling_parameters,
				 agent_id
				 ):
		self.rooms_explored = None
		self.goal_desc = None
		self.agent_id = agent_id
		self.agent_name = "Alice" if agent_id == 0 else "Bob"
		self.oppo_name = "Alice" if agent_id == 1 else "Bob"
		self.oppo_pronoun = "she" if agent_id == 1 else "he"
		self.debug = sampling_parameters.debug
		self.rooms = []
		self.prompt_template_path = prompt_template_path
		self.single = 'single' in self.prompt_template_path
		df = pd.read_csv(self.prompt_template_path)
		self.prompt_template = df['prompt'][0].replace("$AGENT_NAME$", self.agent_name).replace("$OPPO_NAME$", self.oppo_name)
		if communication:
			self.generator_prompt_template = df['prompt'][1].replace("$AGENT_NAME$", self.agent_name).replace("$OPPO_NAME$", self.oppo_name)
		else:
			self.generator_prompt_template = None

		self.communication = communication
		self.cot = cot
		self.source = source
		self.model = None
		self.tokenizer = None
		self.lm_id = lm_id
		self.chat = 'gpt-3.5-turbo' in lm_id or 'gpt-4' in lm_id or 'chat' in lm_id
		self.OPENAI_KEY = None
		self.total_cost = 0

		if self.source == 'openai':
			openai.api_key = os.getenv("OPENAI_KEY")
			if self.chat:
				self.sampling_params = {
					"max_tokens": sampling_parameters.max_tokens,
					"temperature": sampling_parameters.t,
					"top_p": sampling_parameters.top_p,
					"n": sampling_parameters.n,
				}
			else:
				self.sampling_params = {
					"max_tokens": sampling_parameters.max_tokens,
					"temperature": sampling_parameters.t,
					"top_p": sampling_parameters.top_p,
					"n": sampling_parameters.n,
					"logprobs": sampling_parameters.logprobs,
					"echo": sampling_parameters.echo,
				}
		elif self.source == 'hf':
			self.tokenizer = LlamaTokenizer.from_pretrained(self.lm_id, use_fast=True)
			self.model = LlamaForCausalLM.from_pretrained(self.lm_id, device_map='auto', load_in_4bit=True)
			self.sampling_params = {
				"max_new_tokens": sampling_parameters.max_tokens,
				"temperature": sampling_parameters.t,
				"top_p": sampling_parameters.top_p,
				"num_return_sequences": sampling_parameters.n,
				'use_cache': True,
				# 'output_scores': True,
				'return_dict_in_generate': True,
				'do_sample': True,
				# 'early_stopping': True,
			}
		else:
			raise ValueError("invalid source")

		def lm_engine(source, lm_id):

			@backoff.on_exception(backoff.expo, OpenAIError)
			def openai_generate(prompt, sampling_params):
				usage = 0
				try:
					if self.chat:
						response = openai.ChatCompletion.create(
							model=lm_id, messages=prompt, **sampling_params
						)
						# print(json.dumps(response, indent=4))
						if self.debug:
							with open(f"LLM/chat_raw.json", 'a') as f:
								f.write(json.dumps(response, indent=4))
								f.write('\n')
						generated_samples = [response['choices'][i]['message']['content'] for i in
											 range(sampling_params['n'])]
						if 'gpt-4' in self.lm_id:
							usage = response['usage']['prompt_tokens'] * 0.03 / 1000 + response['usage']['completion_tokens'] * 0.06 / 1000
						elif 'gpt-3.5' in self.lm_id:
							usage = response['usage']['total_tokens'] * 0.002 / 1000
					# mean_log_probs = [np.mean(response['choices'][i]['logprobs']['token_logprobs']) for i in
					# 				  range(sampling_params['n'])]
					elif "text-" in lm_id:
						response = openai.Completion.create(model=lm_id, prompt=prompt, **sampling_params)
						# print(json.dumps(response, indent=4))
						if self.debug:
							with open(f"LLM/raw.json", 'a') as f:
								f.write(json.dumps(response, indent=4))
								f.write('\n')
						generated_samples = [response['choices'][i]['text'] for i in range(sampling_params['n'])]
					# mean_log_probs = [np.mean(response['choices'][i]['logprobs']['token_logprobs']) for i in
					# 			  range(sampling_params['n'])]
					else:
						raise ValueError(f"{lm_id} not available!")
				except OpenAIError as e:
					print(e)
					raise e
				return generated_samples, usage

			def tokenize_dialog(dialog):
				B_INST, E_INST = "[INST]", "[/INST]"
				B_SYS, E_SYS = "<<SYS>>\n", "\n<</SYS>>\n\n"
				prompt_tokens = []
				# print(dialog)
				if dialog[0]["role"] == "system":
					dialog = [
								 {
									 "role": dialog[1]["role"],
									 "content": B_SYS
												+ dialog[0]["content"]
												+ E_SYS
												+ dialog[1]["content"],
								 }
							 ] + dialog[2:]
				assert all([msg["role"] == "user" for msg in dialog[::2]]) and all(
					[msg["role"] == "assistant" for msg in dialog[1::2]]
				), (
					"model only supports 'system', 'user' and 'assistant' roles, "
					"starting with 'system', then 'user' and alternating (u/a/u/a/u...)"
				)
				dialog_tokens: List[int] = sum(
					[
						[self.tokenizer.bos_token_id] +
						self.tokenizer.encode(
							f"{B_INST} {(prompt['content']).strip()} {E_INST} {(answer['content']).strip()} ",
							add_special_tokens=False
						)
						+ [self.tokenizer.eos_token_id]
						for prompt, answer in zip(dialog[::2], dialog[1::2], )
					],
					[],
				)
				assert (
						dialog[-1]["role"] == "user"
				), f"Last message must be from user, got {dialog[-1]['role']}"
				dialog_tokens += [self.tokenizer.bos_token_id] + self.tokenizer.encode(
					f"{B_INST} {(dialog[-1]['content']).strip()} {E_INST}", add_special_tokens=False
				)
				prompt_tokens.append(dialog_tokens)
				return torch.tensor(prompt_tokens).to('cuda')
			@torch.inference_mode()
			def hf_generate(prompt, sampling_params):
				if self.chat:
					input_ids = tokenize_dialog(prompt)
				else:
					input_ids = self.tokenizer(prompt, return_tensors="pt").input_ids.to('cuda')
				prompt_len = input_ids.shape[-1]
				output_dict = self.model.generate(input_ids, pad_token_id=self.tokenizer.eos_token_id, # max_length=prompt_len + sampling_params['max_new_tokens'],
											 **sampling_params)
				generated_samples = self.tokenizer.batch_decode(output_dict.sequences[:, prompt_len:])
				generated_samples = [s.strip() for s in generated_samples]
				generated_samples = [s[:-4] if '</s>' in s[-4:] else s for s in generated_samples]
				if self.debug:
					print(generated_samples)
				return generated_samples, 0

			def _generate(prompt, sampling_params):
				usage = 0
				if source == 'openai':
					return openai_generate(prompt, sampling_params)
				elif self.source == 'hf':
					return hf_generate(prompt, sampling_params)
				else:
					raise ValueError("invalid source")

			return _generate

		self.generator = lm_engine(self.source, self.lm_id)

		self.current_room = None
		self.object_list = None
		self.holding_objects = None
		self.obj_per_room = None


	def reset(self, rooms_name, goal_objects):
		self.rooms = rooms_name
		self.goal_desc = self.goal2description(goal_objects)


	def goal2description(self, goals):  # {predicate: count}
		s = "Transport "
		r = None
		for object_name, count in goals.items():
			s += f"{count} {object_name}{'s' if count > 1 else ''}, "

		s = s[:-2] + f" to the bed."
		return s


	def parse_answer(self, available_actions, text):
		flags = 'AC'
		for i in range(len(available_actions)):
			action = available_actions[i]
			if action.startswith("send a message:"):
				action = "send a message"
			if action.lower() in text.lower():
				return available_actions[i], flags
		sents = text.split('\n')  # Split by space
		words = []
		for sent in sents:
			words.extend(sent.split(' '))
		words = list(filter(None, words))  # Remove empty strings from the result

		for i in range(len(available_actions)):
			action = available_actions[i]
			option = chr(ord('A') + i)
			# txt = text.lower()
			if f"option {option}" in text or f"{option}." in words or f"{option}," in words or f"{option}\n" in text.split(" ") or f"Option {option}" in text or f"({option})" in words or f"action {option}" in text or (len(text) <= 2 and option in text):
				return action, flags
		print("WARNING! Fuzzy match!")
		flags = "Fuzzy match"
		for i in range(len(available_actions)):
			action = available_actions[i]
			if self.communication and i == 0:
				continue
			act = "None"
			name = "None"
			id = "None"
			if action.startswith('go to'):
				# act = 'go to'
				name = action.split(' ')[-2][1:-1]
				id = action.split(' ')[-1][1:-1]
			elif action.startswith('explore'):
				act = 'explore'
				name = action.split(' ')[-2][1:-1]
				id = action.split(' ')[-1][1:-1]
			elif action.startswith('go grasp'):
				act = 'grasp'
				name = action.split(' ')[-2][1:-1]
				id = action.split(' ')[-1][1:-1]
			elif action.startswith('put'):
				act = 'put'
			elif action.startswith('transport'):
				act = 'transport'
			option = chr(ord('A') + i)
			if name in text and id in text:
				return action, flags
		for i in range(len(available_actions)):
			action = available_actions[i]
			if self.communication and i == 0:
				continue
			act = "None"
			name = "None"
			id = "None"
			if action.startswith('go to'):
				# act = 'go to'
				name = action.split(' ')[-2][1:-1]
				id = action.split(' ')[-1][1:-1]
			elif action.startswith('explore'):
				act = 'explore'
				name = action.split(' ')[-2][1:-1]
				id = action.split(' ')[-1][1:-1]
			elif action.startswith('go grasp'):
				act = 'grasp'
				name = action.split(' ')[-2][1:-1]
				id = action.split(' ')[-1][1:-1]
			elif action.startswith('put'):
				act = 'put'
			elif action.startswith('transport'):
				act = 'transport'
			option = chr(ord('A') + i)
			if f"{option} " in text or act in text or name in text or id in text:
				return action, flags
		if len(text) == 1:
			i = ord(text) - ord('A')
			if i in range(len(available_actions)):
				return available_actions[i]
		print("WARNING! No available action parsed!!! Random choose one")
		flags = "failed to parse"
		return random.choice(available_actions), flags


	def progress2text(self, current_step, satisfied, opponent_grabbed_objects, opponent_last_room,):
		s = f"I've taken {current_step}/3000 steps. "

		sss = {}
		for room, obj_list in self.obj_per_room.items():
			sr = ""
			s_obj = ""
			s_con = ""
			s_bed = ""
			objs = obj_list[0]
			cons = obj_list[1]
			if len(objs) > 0:
				if len(objs) == 1:
					x = objs[0]
					s_obj += f"a target object <{x['name']}> ({x['id']})"
				else:
					ss = ', '.join([f"<{x['name']}> ({x['id']})" for x in objs])
					s_obj += f"target objects " + ss
			
			if len(cons) > 0:
				if len(cons) == 1:
					x = cons[0]
					s_con = f"a container <{x['name']}> ({x['id']})"
				else:
					ss = ', '.join([f"<{x['name']}> ({x['id']})" for x in cons])
					s_con = f"containers " + ss
			if len(obj_list[2]) > 0:
				s_bed = 'the goal position bed'
			if s_obj == "" and s_con == "" and s_bed == "":
				sr += 'nothing'
			elif s_obj != "" and s_con != "" and s_bed == "":
				sr += s_obj + ', and ' + s_con
			elif s_obj != "" and s_con == "" and s_bed != "":
				sr += s_obj + ', and ' + s_bed
			elif s_obj == "" and s_con != "" and s_bed != "":
				sr += s_con + ', and ' + s_bed
			elif s_obj != "" and s_con != "" and s_bed != "":
				sr += s_obj + ', ' + s_con + ', and ' + s_bed
			else:
				sr += s_obj + s_con + s_bed
			sss[room] = sr
		
		if len(satisfied) == 0:
			if len(self.object_list[2]) == 0:
				s += "I haven't found the goal position bed. "
			else:
				s += ""
		else:
			s += f"{'I' if self.single else 'We'}'ve already transported "
			unique_satisfied = []
			for x in satisfied:
				if x not in unique_satisfied:
					unique_satisfied.append(x)
			if len([x for x in unique_satisfied if x['type'] == 0]) == 0:
				s += 'nothing'
			s += ', '.join([f"<{x['name']}> ({x['id']})" for x in unique_satisfied if x['type'] == 0])
			s += ' to the bed. '

		s_hold = ["", ""]
		for i, obj in enumerate(self.holding_objects):
			if obj['type'] == 0:
				s_hold[i] = f"a target object <{obj['name']}> ({obj['id']}). "
			elif obj['type'] == 1:
				ss = ""
				cnt = 0
				for j, o in enumerate(obj['contained']):
					if o is None:
						break
					cnt += 1
					ss += f"<{obj['contained_name'][j]}> ({o}), "
				if cnt == 0:
					ss = 'nothing'
				else:
					ss = f"target object{'s' if cnt > 1 else ''} {ss[:-2]}"
				s_hold[i] = f"a container <{obj['name']}> ({obj['id']}) with {ss} in it. "

		if self.holding_objects[0]["type"] == 0 and self.holding_objects[1]['type'] == 0:
			s += f"I'm holding two target objects <{self.holding_objects[0]['name']}> ({self.holding_objects[0]['id']}) and <{self.holding_objects[1]['name']}> ({self.holding_objects[1]['id']}). "
		elif s_hold[0] == "" and s_hold[1] == "":
			s += "I'm holding nothing. "
		elif s_hold[0] != "" and s_hold[1] != "":
			s += f"I'm holding {s_hold[0][:-2]}, and {s_hold[1]}"
		else:
			s += f"I'm holding {s_hold[0]}{s_hold[1]}"

		# print(self.current_room, self.obj_per_room)
		if self.current_room not in self.rooms_explored: pred_room = 'none'
		else: pred_room = self.rooms_explored[self.current_room]
		if pred_room != 'all' and sss[self.current_room] == 'nothing':
			s += f"I'm in the {self.current_room}, where I've explored {pred_room} of it. "
		else:
			s += f"I'm in the {self.current_room}, where I've explored {pred_room} of it and found {sss[self.current_room]}. "
		### opponent modeling
		if not self.single:
			s_hold = ["", ""]
			for i, obj in enumerate(opponent_grabbed_objects):
				if obj['type'] == 0:
					s_hold[i] = f"a target object <{obj['name']}> ({obj['id']}). "
				elif obj['type'] == 1:
					ss = ""
					cnt = 0
					for j, o in enumerate(obj['contained']):
						if o is None:
							break
						cnt += 1
						ss += f"<{obj['contained_name'][j]}> ({o}), "
					if cnt == 0:
						ss = 'nothing'
					else:
						ss = f"target object{'s' if cnt > 1 else ''} {ss[:-2]}"
					s_hold[i] = f"a container <{obj['name']}> ({obj['id']}) with {ss} in it. "
			if opponent_grabbed_objects[0]["type"] == 0 and opponent_grabbed_objects[1]['type'] == 0:
				ss = f"two target objects <{opponent_grabbed_objects[0]['name']}> ({opponent_grabbed_objects[0]['id']}) and <{opponent_grabbed_objects[1]['name']}> ({opponent_grabbed_objects[1]['id']}). "
			if s_hold[0] == "" and s_hold[1] == "":
				ss = "nothing. "
			elif s_hold[0] != "" and s_hold[1] != "":
				ss = f"{s_hold[0][:-2]}, and {s_hold[1]}"
			else:
				ss = f"{s_hold[0]}{s_hold[1]}"

			if opponent_last_room is None:
				s += f"I don't know where {self.oppo_name} is. "
			elif opponent_last_room == self.current_room:
				s += f"I also see {self.oppo_name} here in the {self.current_room}, {self.oppo_pronoun} is holding {ss}"
			else:
				s += f"Last time I saw {self.oppo_name} was in the {opponent_last_room}, {self.oppo_pronoun} was holding {ss}"

		for room in self.rooms:
			if room == self.current_room:
				continue
			#s += f"I've explored {self.rooms_explored[room] if room in self.rooms_explored else 'None'} of the {room}, and I found {sss[room]} there. "
			if room not in self.rooms_explored: pred_room = 'none'
			else: pred_room = self.rooms_explored[room]
			if pred_room != 'all' and sss[room] == 'nothing':
				s += f"I've explored {pred_room} of the {room}. "
			else:
				s += f"I've explored {pred_room} of the {room}, and I found {sss[room]} there. "

		return s


	def get_available_plans(self, message):
		"""
		go to room {}
		explore current room {}
		go grasp target object / container {}
		holding both container and object: put obj into the container
		holding any goal objects: transport holding objects to the bed
		send a message: ""
		"""
		available_plans = []
		if self.communication and message is not None:
			available_plans.append(f"send a message: {message}")
		if self.holding_objects[0]['type'] is None or self.holding_objects[1]['type'] is None:
			for obj in self.object_list[0]:
				available_plans.append(f"go grasp target object <{obj['name']}> ({obj['id']})")
			if not (self.holding_objects[0]['type'] == 1 or self.holding_objects[1]['type'] == 1):
				for obj in self.object_list[1]:
					available_plans.append(f"go grasp container <{obj['name']}> ({obj['id']})")
		else:
			if self.holding_objects[0]['type'] == 1 and self.holding_objects[0]['contained'][-1] is None and self.holding_objects[1]['type'] == 0:
				available_plans.append(f"put <{self.holding_objects[1]['name']}> ({self.holding_objects[1]['id']}) into the container <{self.holding_objects[0]['name']}> ({self.holding_objects[0]['id']})")
			elif self.holding_objects[1]['type'] == 1 and self.holding_objects[1]['contained'][-1] is None and self.holding_objects[0]['type'] == 0:
				available_plans.append(f"put <{self.holding_objects[0]['name']}> ({self.holding_objects[0]['id']}) into the container <{self.holding_objects[1]['name']}> ({self.holding_objects[1]['id']})")
		if any(obj['type'] is not None for obj in self.holding_objects) and len(self.object_list[2]) != 0:
			available_plans.append(f"transport objects I'm holding to the bed")
		for room in self.rooms:
			if room == self.current_room or room is None or room == 'None':
				continue
			available_plans.append(f"go to {room}")
		if self.current_room not in self.rooms_explored or self.rooms_explored[self.current_room] != 'all':
			available_plans.append(f"explore current room {self.current_room}")

		plans = ""
		for i, plan in enumerate(available_plans):
			plans += f"{chr(ord('A') + i)}. {plan}\n"

		return plans, len(available_plans), available_plans


	def run(self, current_step, current_room, rooms_explored, holding_objects, satisfied, object_list, obj_per_room, action_history, dialogue_history, opponent_grabbed_objects = None, opponent_last_room = None):
		info = {}
		print("current_step", current_step)
		self.current_room = current_room
		self.rooms_explored = rooms_explored
		self.holding_objects = holding_objects
		self.object_list = object_list
		self.obj_per_room = obj_per_room
		progress_desc = self.progress2text(current_step, satisfied, opponent_grabbed_objects, opponent_last_room)
		action_history_desc = ", ".join(action_history[-10:] if len(action_history) > 10 else action_history)
		dialogue_history_desc = '\n'.join(dialogue_history[-3:] if len(dialogue_history) > 3 else dialogue_history)
		prompt = self.prompt_template.replace('$GOAL$', self.goal_desc)
		prompt = prompt.replace('$PROGRESS$', progress_desc)
		prompt = prompt.replace('$ACTION_HISTORY$', action_history_desc)
		message = None

		if self.communication:
			prompt = prompt.replace('$DIALOGUE_HISTORY$', dialogue_history_desc)
			if not action_history[-1].startswith('send a message'):
				gen_prompt = self.generator_prompt_template.replace('$GOAL$', self.goal_desc)
				gen_prompt = gen_prompt.replace('$PROGRESS$', progress_desc)
				gen_prompt = gen_prompt.replace('$ACTION_HISTORY$', action_history_desc)
				gen_prompt = gen_prompt.replace('$DIALOGUE_HISTORY$', dialogue_history_desc)
				gen_prompt = gen_prompt + f"\n{self.agent_name}:"
				chat_prompt = [{"role": "user", "content": gen_prompt}]
				outputs, usage = self.generator(chat_prompt if self.chat else gen_prompt, self.sampling_params)
				self.total_cost += usage
				message = outputs[0]
				if len(message) > 0 and message[0] != '"':
					message = re.search(r'"([^"]+)"', message)
					if message:
						message = '"' + message.group(1) + '"'
				info['prompt_comm'] = gen_prompt
				info['output_comm'] = outputs
				info['usage_comm'] = usage
				if self.debug:
					print(f"prompt_comm:\n{gen_prompt}")
					print(f"output_comm:\n{message}")

		available_plans, num, available_plans_list = self.get_available_plans(message)
		if num == 0 or (message is not None and num == 1):
			print("Warning! No available plans!")
			plan = None
			info.update({"num_available_actions": num,
					 "plan": None})
			return plan, info

		prompt = prompt.replace('$AVAILABLE_ACTIONS$', available_plans)

		if self.cot:
			prompt = prompt + " Let's think step by step."
			if self.debug:
				print(f"cot_prompt:\n{prompt}")
			chat_prompt = [{"role": "user", "content": prompt}]
			outputs, usage = self.generator(chat_prompt if self.chat else prompt, self.sampling_params)
			output = outputs[0]
			## truncate the unfinished cot
			last_index = output.rfind('.')
			if last_index != -1:
				output = output[:last_index + 1]
			else:
				output += '.'
			self.total_cost += usage
			# info['outputs_cot'] = outputs
			# info['usage_plan_stage_1'] = usage
			if self.debug:
				print(f"output_plan_stage_1:\n{output}")
			chat_prompt = [{"role": "user", "content": prompt},
						   {"role": "assistant", "content": output},
						   {"role": "user", "content": "Answer with only one best next action. So the answer is option"}]
			normal_prompt = prompt + ' ' + output + ' Answer with only one best next action. So the answer is option'
			outputs, usage = self.generator(chat_prompt if self.chat else normal_prompt, self.sampling_params)
			output = outputs[0]
			self.total_cost += usage
			# info['usage_plan_stage_2'] = usage
			if self.debug:
				print(f"output_plan_stage_1:\n{output}")
				print(f"total cost: {self.total_cost}")
		else:
			normal_prompt = prompt
			chat_prompt = [{"role": "user", "content": prompt}]
			if self.debug:
				print(f"base_prompt:\n{prompt}")
			outputs, usage = self.generator(chat_prompt if self.chat else normal_prompt, self.sampling_params)
			output = outputs[0]
			# info['usage_step_1'] = usage
			if self.debug:
				print(f"output_plan_stage_1:\n{output}")
		plan, flags = self.parse_answer(available_plans_list, output)
		if self.debug:
			print(f"plan: {plan}\n")
		info.update({"num_available_actions": num,
					 "prompt_plan_stage_2": normal_prompt,
					 "output_plan_stage_2": output,
					 "parse_exception": flags,
					 "plan": plan,
					 "total_cost": self.total_cost})
		return plan, info

