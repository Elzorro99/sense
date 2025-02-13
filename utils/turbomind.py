import threading
import requests
import time
import json
from utils.logging import logging
import asyncio
import os 
import subprocess
import torch
import gc
import configparser
import signal
import GPUtil
import concurrent.futures
import sys
import aiohttp
import shlex
# Function to check if a string is valid JSON
def is_valid_json(json_str):
    try:
        json.loads(json_str)
        return True
    except json.JSONDecodeError:
        return False

# Function to count the number of GPUs specified in a comma-separated string
def count_gpu(gpus_str):
    gpu_list = gpus_str.split(',')
    return len(gpu_list)

# Function to get the ID of the first GPU from a comma-separated string
def get_first_gpu(gpus_str):
    gpu_list = gpus_str.split(',')
    if len(gpu_list) > 0:
        return int(gpu_list[0])
    else:
        return None

# Function to check a configuration file for a specific tensor parallel size
def check_tp_config(file, tp):
    try:
        config = configparser.ConfigParser()
        config.read(file)
    except FileNotFoundError:
        return False
    
    if 'llama' in config:
        llama_section = config['llama']
        
        if 'tensor_para_size' in llama_section:
            tensor_para_size = int(llama_section['tensor_para_size'])
            if tensor_para_size == tp:
                return True
    
    return False


class TurboMindThread(threading.Thread):
  def __init__(self, *args, **keywords):
    threading.Thread.__init__(self, *args, **keywords)
    self.killed = False
 
  def start(self):
    self.__run_backup = self.run
    self.run = self.__run      
    threading.Thread.start(self)
 
  def __run(self):
    sys.settrace(self.globaltrace)
    self.__run_backup()
    self.run = self.__run_backup
 
  def globaltrace(self, frame, event, arg):
    if event == 'call':
      return self.localtrace
    else:
      return None
 
  def localtrace(self, frame, event, arg):
    if self.killed:
      if event == 'line':
        raise SystemExit()
    return self.localtrace
 
  def stop(self):
    self.killed = True

# Class for managing TurboMind
class TurboMind:
    def __init__(self, instance, model_name: str = None, model_path: str = None, host: str = "127.0.0.1", port: int = 9000, tp: int = 1, instance_num: int = 8, gpu_id=0, warm_up=True, tb_model_type: str = "qwen-14b"):
        instance.models[model_name] = self
        if model_name == "CortexLM|qwen-72b-chat-w4":
            instance.models["Qwen|Qwen-72B-Chat"] = self

        self.headers = {'Content-Type': 'application/json'}
        self.status = 0 # 0 = Not Ready | 1 = Ready
        self.model_name = model_name
        self.model_type = "turbomind"
        self.process = None
        self.instance = instance
        self.model_path = model_path
        self.host = host
        self.port = port
        self.tp = tp
        self.instance_num = instance_num
        self.tb_model_type = tb_model_type
        self.gpu_id = gpu_id
        self.base_directory = instance.base_directory
        self.error_completion_count = 0
        # Load TurboMind Model
        self.run_build_process()
        self.start_process()

    def is_running(self):
        stat = os.system("ps -p %s &> /dev/null" % self.process.pid)
        return stat == 0
    def get_gpu_memory(self, gpu_id):
        try:
            gpu_info = GPUtil.getGPUs()[gpu_id]
            total_memory = gpu_info.memoryUsed
            return total_memory
        except Exception as e:
            logging.error(f"An error occurred while getting GPU memory: {str(e)}")
            return 0.0

    # Function to run an interactive test
    async def run_interactive_test(self):
        async for token in self.interactive_async(prompt="Once upon a time, in a picturesque little village ..."):
            pass
        return
    # Function to process tokens from an interactive test
    async def process_tokens(self, tokens):
        final_response = ""
        for token in tokens:
            final_response += json.loads(token)['text']
        return final_response

    # Function to warm up the TurboMind model
    async def warm_up(self, gpu_id):
        logging.info(f"🌺 Warming up {self.model_path}.. Please wait.")
        logging.warning(f"🌺 This may take some time. We check how many concurrent requests your GPUs can handle.")

        try:
            await self.run_interactive_test()
            single_request_memory = self.get_gpu_memory(get_first_gpu(gpu_id))
            logging.info(f"Single request GPU memory usage: {single_request_memory:.2f} MB")

            await asyncio.gather(
                self.run_interactive_test(),
                self.run_interactive_test()
            )
            total_memory = self.get_gpu_memory(get_first_gpu(gpu_id))
            logging.info(f"Total GPU memory used with two concurrent requests: {total_memory:.2f} MB")

            ram_per_request = (total_memory - single_request_memory) / 2
            logging.info(f"RAM usage per request with two concurrent requests: {ram_per_request:.2f} MB")

        except Exception as e:
            logging.error(f"An error occurred during warming up: {str(e)}")

    # Function to start the TurboMind subprocess
    def start_process(self):
        # Create a thread to run the subprocess
        self.process_thread = threading.Thread(target=self.run_subprocess)
        self.process_thread.start()

    # Function to run the model build process
    def run_build_process(self):
        if not check_tp_config(f"{self.base_directory}{self.model_path}workspace/triton_models/weights/config.ini", count_gpu(self.gpu_id)):
            environment = os.environ.copy()
            environment["CUDA_VISIBLE_DEVICES"] = self.gpu_id
            
            command = f"lmdeploy convert --model-name {self.tb_model_type} --model-path {self.base_directory}{self.model_path}model --dst_path {self.base_directory}{self.model_path}workspace --model-format awq --group-size 128 --tp {count_gpu(self.gpu_id)}"
            logging.info(f'Spawning build model for {self.model_path}')

            try:
                # Execute the command using subprocess.run
                subprocess.run(command, shell=True, check=False, env=environment)
                logging.success(f'Model building is complete')
            except subprocess.CalledProcessError as e:
                logging.error(f"Error when executing the command: {e}")
            except Exception as e:
                logging.error(f"An error occurred: {e}")

    # Function to run the TurboMind subprocess
    def run_subprocess(self):
        environment = os.environ.copy()
        environment["CUDA_VISIBLE_DEVICES"] = self.gpu_id
        
        command = f"lmdeploy serve api_server {self.base_directory}{self.model_path}workspace --server-name {self.host} --server-port {self.port} --tp {count_gpu(self.gpu_id)}"
        logging.info(f'Spawning 1 process for {self.model_path}')

        try:
            # Execute the command using subprocess.run
            subprocess.run(command, shell=True, check=False, env=environment)
        except subprocess.CalledProcessError as e:
            logging.error(f"Error when executing the command: {e}")
        except Exception as e:
            logging.error(f"An error occurred: {e}")

    # Function to wait for the TurboMind model to be ready
    def wait_for_tb_model_status(self, timeout=240):
        start_time = time.time()
        url = f"http://{self.host}:{self.port}/v1/models"
        while True:
            current_time = time.time()
            if current_time - start_time > timeout:
                logging.error(f"Error: Timeout of {timeout} seconds exceeded for model {self.model_path} ({self.host}:{self.port})")
                return False

            try:
                response = requests.get(url)
                if response.status_code == 200:
                    data = response.json()
                    self.tb_model = data['data'][0]['id']
                    logging.info(f'Model {self.model_path} is ready')
                    return True
            except requests.exceptions.RequestException:
                time.sleep(1)
                pass

    # Function for interactive completions
    async def interactive_async(self, prompt=None, temperature=0.7, repetition_penalty=1.2, top_p=0.7, top_k=40, max_tokens=512):
        logging.debug(f"[-->] (Interactive) [{self.model_path}] Request for completion")
        
        payload = {
            "prompt": prompt,
            "temperature": temperature,
            "repetition_penalty": repetition_penalty,
            "top_p": top_p,
            "top_k": top_k,
            "stream": True,
            "request_output_len": max_tokens
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(f"http://{self.host}:{self.port}/v1/chat/interactive", json=payload, headers=self.headers) as response:
                stream_start_time = time.time()
                tokens = 0
                try:
                    async for chunk in response.content.iter_any():
                        chunk = chunk.decode('utf-8')
                        if is_valid_json(chunk):
                            chunk_data = json.loads(chunk)
                            if 'text' in chunk_data:
                                yield json.dumps({"text": chunk_data['text']}) + "\n"
                                tokens = chunk_data['tokens']
                except Exception as e:
                    logging.error('Chunk:', str(e))

                streaming_duration = round(time.time() - stream_start_time, 2)
                logging.debug(f"[<--] (Interactive) [{self.model_path}] Completion done in {streaming_duration}s")

    async def completion_async(self, messages=None, temperature=0.7, repetition_penalty=1.2, top_p=0.7, max_tokens=512, top_k=40):
        logging.debug(f"[-->] [{self.model_path}] Request for completion")
        payload = {
            "model": self.tb_model,
            "messages": messages,
            "temperature": temperature,
            "repetition_penalty": repetition_penalty,
            "top_p": top_p,
            "top_k": top_k,
            "stream": True,
            "max_tokens": max_tokens
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(f"http://{self.host}:{self.port}/v1/chat/completions", json=payload, headers=self.headers) as response:
                stream_start_time = time.time()
                tokens = 0
                try:
                    async for chunk in response.content.iter_any():
                        chunk = chunk.decode('utf-8').replace("data:", "")
                        if is_valid_json(chunk):
                            chunk_data = json.loads(chunk)
                            if 'choices' in chunk_data:
                                if 'content' in chunk_data['choices'][0]["delta"]:
                                    yield json.dumps({"text": chunk_data['choices'][0]["delta"]["content"]}) + "\n"
                except Exception as e:
                    logging.error('Chunk:', str(e))
                streaming_duration = round(time.time() - stream_start_time, 2)
                logging.debug(f"[<--] (Completion) [{self.model_path}] Completion done in {streaming_duration}s")

    async def destroy(self):
        if self.process:
            try:
                logging.info(f"Stop {self.model_path} model..")
                del self.instance.models[self.model_name]
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                await asyncio.sleep(2)
                logging.info(f"{self.model_path} model stopped.")
            except Exception as e:
                logging.error(f"Error when stopping {self.model_path} model: {e}")
        else:
            logging.info(f"{self.model_path} model is not running.")
