#!/usr/bin/env python


import sys
import time
import subprocess
import jobs_client


def launch_app_server():
    server_process = subprocess.Popen(["python", "./jobs_app.py"], shell=True)
    return server_process


def stop_app_server(server_process):
    print("Stopping server...", flush=True)
    server_process.terminate()
    server_process.kill()
    server_process.wait()
    print("server stopped.", flush=True)


def test_enqueue_and_wait():
    srv = launch_app_server()
    time.sleep(1)
    url = "http://localhost:5000/"
    job = jobs_client.enqueue_url(lambda: "OK!", url)
    result = jobs_client.wait_url(job["jobid"], url)
    print(result)
    stop_app_server(srv)
    assert(result["result"] == "OK!")


def test_job_context():
    srv = launch_app_server()
    time.sleep(1)
    url = "http://localhost:5000/"

    def func(__context__):
        print(__context__)
        return __context__["jobid"]

    job = jobs_client.enqueue_url(func, url)
    result = jobs_client.wait_url(job["jobid"], url, timeout=2)
    print(result)
    stop_app_server(srv)
    assert(result["result"] == 0)


def test_deps_simple():
    srv = launch_app_server()
    time.sleep(1)
    url = "http://localhost:5000/"

    job1 = jobs_client.enqueue_url(lambda: 1+2, url)
    job2 = jobs_client.enqueue_url(lambda: 1+2, url, deps=[job1["jobid"]])
    job3 = jobs_client.enqueue_url(lambda: 1+2, url, deps=[job2["jobid"]])
    results = [jobs_client.wait_url(j["jobid"], url)["result"] for j in (job1, job2, job3)]
    print(results)

    print("result:", sum(results))
    print("expected result:", 9)
    stop_app_server(srv)
    return sum(results) == 9


def test_continuation():
    srv = launch_app_server()
    time.sleep(1)
    url = "http://localhost:5000/"

    def func(n):
        #nonlocal __jobid__
        print("func:", n, file=sys.stderr, flush=True)
        if n < 2: return 1
        else:
            rest = jobs_client.enqueue_url(lambda: func(n-1))
            cont = lambda n=n: jobs_client.wait_url(rest["jobid"]) + n
            #jobs_client.requeue_url(

    #job1 = jobs_client.enqueue_url(lambda: 1+2, url)
    #job2 = jobs_client.enqueue_url(lambda: 1+2, url, deps=[job1["jobid"]])
    #job3 = jobs_client.enqueue_url(lambda: 1+2, url, deps=[job2["jobid"]])
    #results = [jobs_client.wait_url(j["jobid"], url)["result"] for j in (job1, job2, job3)]
    #print(results)

    #print("result:", sum(results))
    #print("expected result:", 9)
    #stop_app_server(srv)
    #return sum(results) == 9
    assert(result == 3)
