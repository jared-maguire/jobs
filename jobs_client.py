#!/usr/bin/env python


import base64
import requests
import json
import sys
import dill as pickle


def enqueue_url(func, url, deps=[], timeout=10):
    code = base64.b64encode(pickle.dumps(func)).decode("utf-8")
    obj = {'code': code, "deps": deps}
    response = requests.post(url + "enqueue", json=obj)
    result = json.loads(response.content)
    print(result)
    return result


def wait_url(jobid, url, timeout=10):
    obj = {'jobid': jobid}
    response = requests.post(url + "wait", json=obj, timeout=timeout)
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

