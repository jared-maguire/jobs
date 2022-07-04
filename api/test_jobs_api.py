#!/usr/bin/env python


import sys
import time
import subprocess
import jobs
import jobs_client
import requests
from requests.exceptions import ConnectionError
import multiprocessing
import functools


def launch_app_server():
    server_process = subprocess.Popen(["python", "./jobs_app.py"], shell=True)
    return server_process


def stop_app_server(server_process, url="http://localhost:5000/"):
    print("Stopping server...", flush=True)
    jobs_client.shutdown_server(url)
    print("Waiting...", flush=True)
    server_process.wait()
    print("server stopped.", flush=True)


def test_start_and_stop():
    srv = launch_app_server()
    url = "http://localhost:5000/"
    time.sleep(5)
    stop_app_server(srv)
    try:
        jobs_client.enqueue_url(lambda: "OK!", url, timeout=10)
    #except ConnectionError as err:
    except: 
        return True
    assert(False)


def test_enqueue_and_wait():
    srv = launch_app_server()
    time.sleep(1)
    url = "http://localhost:5000/"
    job = jobs_client.enqueue_url(lambda: "OK!", url)
    result = jobs_client.wait_url(job["jobid"], url)
    print("oh boy result!:", result)
    stop_app_server(srv)
    assert(result["result"] == "OK!")


def test_map():
    srv = launch_app_server()
    time.sleep(1)
    url = "http://localhost:5000/"
    thunks = [lambda: "OK!" for i in range(10)]
    jobs = jobs_client.map_url(thunks, url)
    results = [jobs_client.wait_url(jobid, url, timeout=5, retries=5) for jobid in jobs["jobids"]]
    print("oh boy results!:", results)
    stop_app_server(srv)
    print(results)
    for result in results:
        assert(result is not None)
        assert(result["result"] == "OK!")


def test_job_context():
    srv = launch_app_server()
    time.sleep(1)
    url = "http://localhost:5000/"

    def func(__context__):
        print(__context__)
        return __context__["jobid"]

    jobs = [jobs_client.enqueue_url(func, url)["jobid"] for i in range(10)]
    results = tuple([jobs_client.wait_url(j, url, timeout=2)["result"] for j in jobs])
    print(results)
    stop_app_server(srv)
    assert(results == (0,1,2,3,4,5,6,7,8,9))


def test_deps_simple():
    srv = launch_app_server()
    time.sleep(1)
    url = "http://localhost:5000/"

    job1 = jobs_client.enqueue_url(lambda: 1+2, url)
    job2 = jobs_client.enqueue_url(lambda: 1+2, url, deps=[job1["jobid"]])
    job3 = jobs_client.enqueue_url(lambda: 1+2, url, deps=[job2["jobid"]])
    results = [jobs_client.wait_url(j["jobid"], url)["result"] for j in (job3, job2, job1)]
    print(results)

    print("result:", sum(results))
    print("expected result:", 9)
    stop_app_server(srv)
    assert(sum(results) == 9)
    return sum(results) == 9


def test_jobwaitforjob():
    srv = launch_app_server()
    time.sleep(1)
    url = "http://localhost:5000/"

    # __context__ is a special variable containing job context information.
    # It gets populated by the Dispatcher, if the function takes it as a parameter.
    def func1(__context__):
        return 1

    def func2(jobid, __context__):
        return 1 + jobs_client.wait_url(jobid, url) 

    job1 = jobs_client.enqueue_url(func1, url)
    job2 = jobs_client.enqueue_url(lambda j=job1["jobid"]: functools.partial(jobid=j), url)
    result = jobs_client.wait_url(job1["jobid"], url)
    result = jobs_client.wait_url(job2["jobid"], url)
    print("test_jobwaitforjob:", result)
    stop_app_server(srv, url)
    assert(result["result"] == 2)


def test_continuation():
    srv = launch_app_server()
    time.sleep(1)
    url = "http://localhost:5000/"

    # __context__ is a special variable containing job context information.
    # It gets populated by the Dispatcher, if the function takes it as a parameter.
    def func(__context__):
        rest = jobs_client.enqueue_url(lambda: 1, url)
        cont = lambda jid=rest["jobid"]: jobs_client.wait_url(jid)["result"] + 1
        #jobs_client.requeue_url(__context__["jobid"], cont, url, deps=[rest["jobid"]])
        jobs_client.enqueue_url(cont, url, deps=[rest["jobid"]])

    job = jobs_client.enqueue_url(lambda: func(), url)
    result = jobs_client.wait_url(0, url)
    result = jobs_client.wait_url(2, url)
    stop_app_server(srv, url)
    assert(result["result"] == 2)
