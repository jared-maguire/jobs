#!/usr/bin/env python

import os
import dill as pickle
import tempfile
import inspect
import traceback
from queue import Queue
import threading
import time
import multiprocessing
from multiprocessing.pool import ThreadPool
import requests
import base64
import json


def mktemp():
    f = tempfile.NamedTemporaryFile()
    fname = f.name
    f.close()
    return fname


def pickle_func(func):
    fname = mktemp()
    with open(fname, "wb") as fp:
        pickle.dump(func, fp)
    return fname    


def unpickle_func(fname):
    with open(fname, "rb") as fp:
        func = pickle.load(fp)
    return func    


######

def enqueue_url(func, url, deps=[]):
    code = base64.b64encode(pickle.dumps(func)).decode("utf-8")
    obj = {'code': code, "deps": deps}
    response = requests.post(url + "enqueue", json=obj)
    result = json.loads(response.content)
    print(result)
    return result


def wait_url(jobid, url):
    obj = {'jobid': jobid}
    response = requests.post(url + "wait", json=obj)
    result = json.loads(response.content)
    print(result)
    return result


def check_url(jobid, url):
    obj = {'jobid': jobid}
    response = requests.post(url + "check", json=obj)
    result = response.content
    print(result)
    return result


def requeue_url(jobid, func, url, deps=[]):
    code = base64.b64encode(pickle.dumps(func)).decode("utf-8")
    obj = {'jobid': jobid, 'code': code, "deps": deps}
    response = requests.post(url + "requeue", json=obj)
    jobid = response.content
    print(jobid)
    return jobid
    

####


def load_and_execute_file(fname, *args, **kwargs):
    func = unpickle_func(fname)
    return func(*args, **kwargs)


def picklemap(funcs):
    temps = [mktemp() for i in range(len(funcs))]

    for i, temp in enumerate(temps):
        with open(temp, "wb") as fp:
            pickle.dump(funcs[i], fp)

    results = [load_and_execute_file(temp) for temp in temps]

    map(os.unlink, temps)

    return results


def make_thunk(func, *args, **kwargs):
    return lambda args=args, kwargs=kwargs: func(*args, **kwargs)


def fib(a):
    if a == 0: return 0
    if a == 1: return 1
    if a == 2: return 2
    else: return fib(a-1) + fib(a-2)


def test1():
    expected = tuple(range(10))
    result = tuple(picklemap([lambda a=i: a for i in range(10)]))
    if result == expected:
        print(tuple(range(10)), "==", result)
        return True
    else:
        print(tuple(range(10)), "!=", result)
        return False



class Dispatcher:
    def __init__(self):
        self.all_jobs = dict()
        self.pending = set()
        self.ready = set()
        self.running = set()
        self.done = dict()
        self.jobid = 0
        self.keep_running = True
        self.pool = ThreadPool()
        self.lock = threading.Lock()

    class Continuation:
        def __init__(self, jobid, thunk, deps=[]):
            if callable(thunk):
                thunk = pickle.dumps(thunk)
            self._thunk = thunk
            self.jobid = jobid
            self.deps = deps

        @property
        def thunk(self):
            if self._thunk.__class__ == bytes:
                return pickle.loads(self._thunk)
            elif callable(self._thunk):
                return self._thunk
            else:
                print(self._thunk.__class__)
                assert(False)

        def __str__(self):
            return f"Continuation({self.jobid}, {self.deps})"

    def dump(self):
        print("--")
        print("pending", len(self.pending), list(map(str, self.pending)))
        print("ready", len(self.ready), self.ready)
        print("running", len(self.running), self.running)
        print("done", len(self.done), self.done)
        print("keep_running", self.keep_running)
        print("--")

    def enqueue(self, thunk, deps=[]):
        self.lock.acquire()
        record = Dispatcher.Continuation(self.jobid, thunk, tuple(deps))
        #print("Dispatcher.enqueue", record.source)
        self.jobid += 1
        self.all_jobs[record.jobid] = record
        self.pending.add(record)
        self.lock.release()
        return record.jobid

    def requeue(self, jobid, thunk, deps=[]):
        self.lock.acquire()
        record = Dispatcher.Continuation(jobid, thunk, tuple(deps))
        self.all_jobs[record.jobid] = record
        self.pending.add(record)
        self.lock.release()
        return record.jobid

    def check_ready(self, jobid):
        thunk = self.all_jobs[jobid].thunk
        deps = self.all_jobs[jobid].deps
        if False in [self.check(dep) for dep in deps]:
            return False
        else:
            return True

    def wait(self, jobid):
        while(jobid not in self.done):
            #self.dump()
            time.sleep(1)
        return self.done[jobid]

    def check(self, jobid):
        return jobid in self.done

    def catch_result(self, jobid, result):
        #print(f"Dispatcher finished job {jobid}, result: {result}", flush=True)
        if result.__class__ == Dispatcher.Continuation:
            #print("set_done", f"job {jobid} not complete, requeueing continuation", flush=True)
            self.requeue(jobid, result.thunk, result.deps)
            return
        self.done[jobid] = result

    def step(self):
        #print("Dispatcher.run, beat", len(self.ready), flush=True)
        self.lock.acquire()
        #self.dump()
        # 1. Move and jobs from pending to ready
        for record in list(self.pending):
            if self.check_ready(record.jobid):
                self.pending.discard(record)
                self.ready.add(record)
        # 2. Launch any ready jobs
        if len(self.ready) != 0:
            #print("Dispatcher.run:", len(self.ready), flush=True)
            if len(self.ready) == 0:
                #print("Nothing ready...", flush=True)
                pass
            else:
                record = self.ready.pop()
                #print(f"Dispatcher running job {record.jobid}, {record.thunk}", flush=True)
                self.pool.apply_async(record.thunk, callback=lambda result, j=record.jobid: self.catch_result(j, result))
                self.running.add(record)
        self.lock.release()

    def run(self):
        while self.keep_running:
            self.step()
            time.sleep(0.1)

    def start(self):
        self.thread = threading.Thread(target=self)
        self.keep_running = True
        self.thread.start()

    def stop(self):
        #while len(self.pending) + len(self.ready) != 0:
        #    print("Stopping...")
        #    self.dump()
        #    time.sleep(1)
        #    self.stop()
        self.keep_running = False
        self.pool.close()

    def __call__(self):
        return self.run()


def test2():
    dispatcher = Dispatcher()
    dispatcher.start()
    job = dispatcher.enqueue(lambda: 1+2)
    result = dispatcher.wait(job)
    dispatcher.stop()
    print("result:", result)
    print("expected result:", 3)
    return result == 3


def test2a():
    dispatcher = Dispatcher()
    dispatcher.start()

    job1 = dispatcher.enqueue(lambda: 1+2)
    job2 = dispatcher.enqueue(lambda: 1+2, deps=[job1])
    job3 = dispatcher.enqueue(lambda: 1+2, deps=[job2])
    results = list(map(dispatcher.wait, (job1, job2, job3)))

    dispatcher.stop()
    print("result:", sum(results))
    print("expected result:", 9)
    return sum(results) == 9


def test2b():
    dispatcher = Dispatcher()
    dispatcher.start()

    job1 = dispatcher.enqueue(lambda: 1+2)
    job2 = dispatcher.enqueue(lambda: 1+2+dispatcher.wait(job1), deps=[job1])
    job3 = dispatcher.enqueue(lambda: 1+2+dispatcher.wait(job2), deps=[job2])
    results = list(map(dispatcher.wait, (job1, job2, job3)))

    dispatcher.stop()
    print("result:", sum(results))
    print("expected result:", 18)
    return sum(results) == 18


def test2a_url():
    url = "http://localhost:5000/"

    job1 = enqueue_url(lambda: 1+2, url)
    job2 = enqueue_url(lambda: 1+2, url, deps=[job1["jobid"]])
    job3 = enqueue_url(lambda: 1+2, url, deps=[job2["jobid"]])
    results = [wait_url(j["jobid"], url)["result"] for j in (job1, job2, job3)]
    print(results)

    print("result:", sum(results))
    print("expected result:", 18)
    return sum(results) == 18


def test2b_url():
    url = "http://localhost:5000/"

    job1 = enqueue_url(lambda: 1+2, url)
    job2 = enqueue_url(lambda: 1+2+wait_url(job1["jobid"], url)["result"], url, deps=[job1["jobid"]])
    job3 = enqueue_url(lambda: 1+2+wait_url(job2["jobid"], url)["result"], url, deps=[job2["jobid"]])
    results = [wait_url(j["jobid"], url)["result"] for j in (job1, job2, job3)]
    print(results)

    print("result:", sum(results))
    print("expected result:", 18)
    return sum(results) == 18


def test3():
    dispatcher = Dispatcher()
    dispatcher.start()

    def _sum(lst):
        if len(lst) == 0: return 0
        elif len(lst) == 1: return lst[0]
        else: return lst[0] + _sum(lst[1:])

    job = dispatcher.enqueue(lambda: sum([1,2,3]))
    result = dispatcher.wait(job)
    dispatcher.stop()
    print("result:", result)
    print("expected result:", 6)
    return result == 6


def test3a():
    dispatcher = Dispatcher()
    dispatcher.start()

    def _sum(lst):
        #print("sum!", lst, flush=True)
        if len(lst) == 0: return 0
        elif len(lst) == 1: return lst[0]
        else:
            #print(f"sum: forking {lst}", flush=True)
            jobid = dispatcher.enqueue(lambda: _sum(lst[1:]))
            #print(f"sum: jobid: {jobid}", flush=True)
            return Dispatcher.Continuation(None, lambda: dispatcher.wait(jobid) + lst[0], deps=[jobid])

    job = dispatcher.enqueue(lambda: _sum([1,2,3]))
    result = dispatcher.wait(job)
    dispatcher.stop()
    print("result:", result)
    print("expected result:", 6)
    return result == 6



def test4():
    dispatcher = Dispatcher()
    dispatcher.start()

    def _fib(n):
        if n == 0: return 0
        if n == 1: return 1
        if n == 2: return 2
        else: 
            job1 = dispatcher.enqueue(lambda: _fib(n-1))
            job2 = dispatcher.enqueue(lambda: _fib(n-2))
            return Dispatcher.Continuation(None, lambda: dispatcher.wait(job1) + dispatcher.wait(job2), deps=[job1, job2])

    job = dispatcher.enqueue(lambda: _fib(6))
    result = dispatcher.wait(job)
    dispatcher.stop()
    print("result:", result)
    print("expected result:", fib(6))
    return result == fib(6)


tests = [test1, test2, test2a, test2b]
def run_tests():
    return [(lambda: (print(">> Running Test:", test.__name__), test())[1])() for test in tests]


if __name__=="__main__":
    #print("TEST", "test3", test3())
    print("TEST", "test4", test4())
