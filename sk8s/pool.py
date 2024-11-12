from flask import Flask, request, jsonify
import requests
import sys
import dill as pickle  # Use dill for enhanced pickling capabilities
import multiprocessing
import functools
import uuid
from threading import Thread
import time
from queue import Queue

import sk8s.services
import sk8s.util


def run_task(task):
    func = sk8s.util.deserialize_func(task)
    return func() 


class TaskManager:
    def __init__(self, kvs_service=None):
        self.tasks_remote = sk8s.services.KVSClient(kvs_service) if kvs_service else dict()
        self.tasks = dict()
        self.futures = dict()
        self.queue = Queue()
        self.pool = multiprocessing.Pool()
        self.close_flag = False
        self.thread = Thread(target=self.monitor_thread, daemon=True)    
        self.thread.start()

    def monitor_thread(self):
        while True:

            if self.close_flag: return None

            for task_id in list(self.tasks.keys()):
                print("Task ID:", task_id, self.tasks[task_id]['status'], flush=True)

                # KVS doesn't handle nested dicts
                task_info = self.tasks[task_id]

                if task_info['status'] == 'PENDING':
                    task_info['status'] = 'RUNNING'
                    self.futures[task_id] = self.pool.apply_async(run_task, args=(self.tasks[task_id]['code'],))
                elif task_info['status'] == 'RUNNING':
                    if self.futures[task_id].ready():
                        task_info['status'] = 'COMPLETE'
                        task_info['result'] = self.futures[task_id].get()

                # KVS doesn't handle nested dicts
                self.tasks[task_id ] = task_info
                self.tasks_remote[task_id] = task_info

            time.sleep(1)

    def submit_task(self, code):
        #print("Submitting task", code)
        task_id = str(uuid.uuid4())
        self.tasks[task_id] = dict(task_id=task_id, code=code, status='PENDING')
        self.queue.put(task_id)
        return task_id

    def get_task_status(self, task_id):
        #print("Getting task status", task_id, self.tasks[task_id]['status'])
        return self.tasks[task_id]['status']
    
    def get_task_result(self, task_id):
        #print("Getting task result", task_id, self.tasks[task_id]['result'])    
        return self.tasks[task_id]['result']
   
    def close(self):
        self.close_flag = True
        self.pool.close()
        self.pool.join()
        self.thread.join()
    

class WorkerService:
    def __init__(self, kvs=None, host='0.0.0.0', port=5000, debug=False):
        self.app = Flask(__name__)
        self.tm = TaskManager(kvs_service=kvs)
        self.app.add_url_rule('/submit', 'submit', self.submit, methods=['POST'])
        self.app.add_url_rule('/status/<tid>', 'status', self.status, methods=['GET'])
        self.app.add_url_rule('/result/<tid>', 'result', self.result, methods=['GET'])
        self.app.add_url_rule('/close', 'close', self.close, methods=['GET'])
        self.app.run(host=host, port=port)    

    def submit(self):
        code = request.json['code']
        task_id = self.tm.submit_task(code)
        return jsonify(task_id)

    def status(self, tid):
        return jsonify(self.tm.get_task_status(tid))

    def result(self, tid):
        return jsonify(self.tm.get_task_result(tid))

    def shutdown(self):
        time.sleep(5)
        self.tm.close()
        sys.exit(0)

    def close(self):
        # So weird.
        thread = Thread(target=self.shutdown, daemon=True)
        thread.start()
        return jsonify('OK') 


class Pool:
    def __init__(self, n_workers, config=None, image=None, serviceAccountName=None):
        if config is None:
            config = sk8s.configs.load_config()

        if image is None:
            image = config["docker_image_prefix"] + "jobs"

        if serviceAccountName is None:
            serviceAccountName = config["service_account_name"]

        self.kvs_service = sk8s.services.kvs_service()
        self.kvs = sk8s.services.KVSClient(self.kvs_service)

        self.services = [sk8s.services.service(lambda kvs=self.kvs_service: WorkerService(kvs), config=config, image=image, serviceAccountName=serviceAccountName)
                         for i in range(n_workers)]

        self.forwards = [sk8s.services.forward(s, 5000) for s in self.services]

        self.tid_worker_map = dict()

        self.current_worker = 0

    def close(self):
        for s, f in zip(self.services, self.forwards):
            if f.proc is not None:
                f.proc.kill()
            sk8s.services.shutdown_service(s)
        sk8s.services.shutdown_service(self.kvs_service)
            
    def __del__(self):
        self.close()

    def run(self, func, *args):
        code = sk8s.util.serialize_func(lambda a=args: func(*a))

        # POST the code to the service
        req = requests.post(self.forwards[self.current_worker].url + '/submit', json={'code': code})
        tid = req.json()

        self.tid_worker_map[tid] = self.current_worker
        self.current_worker = (self.current_worker + 1) % len(self.services)

        return tid
    
    def map(self, func, args_list):
        tids = [self.run(func, arg) for arg in args_list]
        return tids

    def starmap(self, func, args_list):
        tids = [self.run(func, *args) for args in args_list]
        return tids

    def get_status(self, tid):
        return self.kvs[tid]['status']

    def get_result(self, tid):
        return self.kvs[tid]['result']
        
    def wait(self, tids, timeout=None):
        if tids.__class__ == str:
            tids = [tids] 
        results = [None] * len(tids)

        start_time = time.time()        

        for idx, tid in enumerate(tids):
            while True:
                status = self.get_status(tid)
                if status == 'COMPLETE':
                    result = self.get_result(tid)
                    results[idx] = result
                    break

                if timeout is not None and time.time() - start_time > timeout:
                    return results

                time.sleep(1)
        return results if len(results) > 1 else results[0]
        