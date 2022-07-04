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
import sys
import functools
import string
import random
import jinja2
import subprocess


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


def run_job_k8s(thunk):
    pass


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

        @property
        def proc(self):
            # This is eldritch:
            thunk = self.thunk
            if "__context__" in thunk.__code__.co_varnames:
                context = dict(jobid=self.jobid, deps=self.deps)
                return functools.partial(thunk, __context__=context)
            else:
                return self.thunk

        def __str__(self):
            return f"Continuation({self.jobid}, {self.deps})"

    def dump(self, fp=sys.stdout):
        print("--", file=fp)
        print("pending", len(self.pending), list(map(str, self.pending)), file=fp)
        print("ready", len(self.ready), self.ready, file=fp)
        print("running", len(self.running), list(map(str, self.running)), file=fp)
        print("done", len(self.done), self.done, file=fp)
        print("keep_running", self.keep_running, file=fp)
        print("--", file=fp)

    def enqueue(self, thunk, deps=[]):
        self.lock.acquire()
        record = Dispatcher.Continuation(self.jobid, thunk, tuple(deps))
        print("Dispatcher.enqueue", str(record), file=sys.stderr)
        self.jobid += 1
        self.all_jobs[record.jobid] = record
        self.pending.add(record)
        self.lock.release()
        return record.jobid

    def map(self, thunks):
        jobids = list(map(self.enqueue, thunks))
        return jobids

    def requeue(self, jobid, thunk, deps=[]):
        self.lock.acquire()
        record = Dispatcher.Continuation(jobid, thunk, tuple(deps))
        print("Dispatcher.requeue", str(record), file=sys.stderr)
        self.all_jobs[record.jobid] = record
        self.pending.add(record)
        self.lock.release()
        return record.jobid

    def check_ready(self, jobid):
        deps = self.all_jobs[jobid].deps
        if False in [self.check(dep) for dep in deps]:
            return False
        else:
            return True

    def wait(self, jobid):
        while(jobid not in self.done):
            #self.dump()
            time.sleep(0.1)
        return self.done[jobid]

    def check(self, jobid):
        return jobid in self.done

    def catch_result(self, record, result):
        jobid = record.jobid
        print(f"Dispatcher finished job {record}, result: {result}", flush=True, file=sys.stderr)
        if result.__class__ == Dispatcher.Continuation:
            #print("set_done", f"job {jobid} not complete, requeueing continuation", flush=True)
            self.requeue(jobid, result.thunk, result.deps)
            self.running.discard(record)
            return
        self.done[jobid] = result
        self.running.discard(record)

    def step(self):
        #print("Dispatcher.run, beat", len(self.ready), flush=True)
        self.lock.acquire()
        state_changed = False
        # 1. Move and jobs from pending to ready
        for record in list(self.pending):
            if self.check_ready(record.jobid):
                self.pending.discard(record)
                self.ready.add(record)
                state_changed = True
        # 2. Launch any ready jobs
        if len(self.ready) != 0:
            #print("Dispatcher.run:", len(self.ready), flush=True)
            if len(self.ready) == 0:
                #print("Nothing ready...", flush=True)
                pass
            else:
                record = self.ready.pop()
                #print(f"Dispatcher running job {record.jobid}, {record.thunk}", flush=True)
                self.pool.apply_async(record.proc, callback=lambda result, rec=record: self.catch_result(rec, result))
                self.running.add(record)
                state_changed = True
        if state_changed:
            self.dump(fp=sys.stderr)
        self.lock.release()

    def run(self):
        while self.keep_running:
            self.step()
            time.sleep(1.0)

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


if __name__=="__main__":
    #print("TEST", "test3", test3())
    print("TEST", "test4", test4())