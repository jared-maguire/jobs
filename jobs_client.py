#!/usr/bin/env python


import base64
import requests
import json
import sys
import dill as pickle
import inspect


def enqueue_url(func, url, deps=[], timeout=10):
    print("API enqueue: ", inspect.getsource(func), deps, file=sys.stderr)
    code = base64.b64encode(pickle.dumps(func)).decode("utf-8")
    obj = {'code': code, "deps": deps}
    response = requests.post(url + "enqueue", json=obj)
    result = json.loads(response.content)
    print(result)
    return result


def map_url(funcs, url, deps=[], timeout=10):
    print("API map: ", [inspect.getsource(func) for func in funcs], deps, file=sys.stderr)
    code = [base64.b64encode(pickle.dumps(func)).decode("utf-8") for func in funcs]
    obj = {'code': code, "deps": deps}
    response = requests.post(url + "map", json=obj)
    result = json.loads(response.content)
    print(result)
    return result


def wait_url(jobid, url, timeout=1, retries=None):
    print("API wait: ", jobid, file=sys.stderr)
    obj = {'jobid': jobid}

    tries = 0
    while True:
        print("wait_url: Waiting on", obj)

        if (tries > 0) and (retries is None):
            return None

        if (retries is not None) and (tries > retries):
            return None

        try:
            if retries is not None and tries < retries:
                response = requests.post(url + "wait", json=obj, timeout=timeout)
                tries += 1
                break
            elif retries is not None and tries >= retries:
                return None
            elif retries is None:
                response = requests.post(url + "wait", json=obj, timeout=timeout)
                tries += 1
                print("WTF")
                break
        except requests.exceptions.Timeout as e:
            print("wtf", e)
            tries += 1
            continue
        except Exception as e:
            raise e
        break
    result = json.loads(response.content)
    print(result)
    print("wait_url: Done Waiting on", obj)
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


def shutdown_server(url):
    try:
        requests.get(url + "shutdown", timeout=1)
    except requests.exceptions.ConnectionError: 
        pass
    return None
