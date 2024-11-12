#!/usr/bin/env python

import sk8s
import time


def test_tm():
    tm = sk8s.TaskManager()
    tid = tm.submit_task(sk8s.util.serialize_func(lambda: 'OK'))
    while tm.get_task_status(tid) != 'COMPLETE':
        time.sleep(1)
    print("Complete! Result:", tm.get_task_result(tid))
    tm.close()


def test_tm_with_kvs():
    kvs_service = sk8s.services.kvs_service()
    tm = sk8s.TaskManager(kvs_service=kvs_service)
    tid = tm.submit_task(sk8s.util.serialize_func(lambda: 'OK'))
    while tm.get_task_status(tid) != 'COMPLETE':
        time.sleep(1)
    assert(tm.get_task_result(tid) == 'OK')
    tm.close()


def test_WorkerService():
    from threading import Thread

    thread = Thread(target=sk8s.WorkerService, daemon=True)
    thread.start()
    
    time.sleep(5)
    
    # Submit a task
    import requests
    r = requests.post('http://localhost:5000/submit', json={'code': sk8s.serialize_func(lambda: 'OK')})
    tid = r.json()
   
    # Wait for the task to complete 
    while True:
        r = requests.get(f'http://localhost:5000/status/{tid}')
        if r.json() == 'COMPLETE':
            break
        time.sleep(1)

    # Get the result
    r = requests.get(f'http://localhost:5000/result/{tid}')
    print("Result:", r.json())
    
    # Close the service
    r = requests.get('http://localhost:5000/close')
    print("Closed service")
    
    print("Done")  

    
def test_kvs_local():
    kvs_service = sk8s.services.kvs_service()
    kvs_client = sk8s.services.KVSClient(kvs_service)

    kvs_client.put('key', 'value')
    value = kvs_client.get('key')

    sk8s.services.shutdown_service(kvs_service)
    
    assert(value == 'value')


def test_kvs_remote():
    def f():
        kvs_service = sk8s.services.kvs_service()
        kvs_client = sk8s.services.KVSClient(kvs_service)
        kvs_client.put('key', 'value')
        result = kvs_client.get('key')
        sk8s.services.shutdown_service(kvs_service)
        return result

    job = sk8s.run(f)
    result = sk8s.wait(job)

    assert(result == 'value')


def test_Pool_run():
    pool = sk8s.Pool(2)

    print("Pool KVS service:", pool.kvs_service, flush=True)

    tid = pool.run(lambda: 'OK')
    result = pool.wait(tid)
    assert(result=="OK")

    pool.close()    
    
    
def test_Pool_map():
    pool = sk8s.Pool(3)
    tids = pool.map(lambda x: x * 2, range(20))
    results = pool.wait(tids)
    for a, b in zip(range(10), results):
        assert(a * 2 == b)


if __name__ == '__main__':
    #test_tm()
    #test_tm_with_kvs()
    #test_kvs_local()
    #test_kvs_remote()
    #test_WorkerService()
    test_Pool_run()
