ps ux | grep port\ 3063 | awk {'print $2'} | xargs kill

python3 tdw-gym/challenge.py \
--output_dir results \
--run_id 11001 \
--port 3063 \
--agents h_agent \
--prompt_template_path LLM/prompt_nocom.csv \
--max_tokens 256 \
--cot \
--lm_id gpt-4 \
--max_frames 3000 \
--data_prefix dataset/dataset_test/ \
--debug \
--screen_size 512 \
--no_gt_mask

ps ux | grep port\ 3063 | awk {'print $2'} | xargs kill