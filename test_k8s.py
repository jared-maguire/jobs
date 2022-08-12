#!/usr/bin/env python

import k8s


## Cluster

def test_cluster_config():
    assert(k8s.check_cluster_config())


## Jobs

def test_run_and_wait():
    result = k8s.wait(k8s.run(lambda: "Hooray"), timeout=30)
    assert(result=="Hooray")


def test_run_and_wait_2():
    result = k8s.run(lambda: "Hooray", nowait=False, timeout=30)
    assert(result=="Hooray")


def test_fail():
    import os
    try:
        job = k8s.run(lambda: 1/0)
        job = k8s.wait(job)
    except RuntimeError:
        assert(os.system(f"kubectl delete job {job}") == 0)
        return
    assert(False)


def test_map():
    results = k8s.map(lambda i: i*2, (0,1,2))
    assert(tuple(results) == (0,2,4))


def test_imports():
    def pi():
        import numpy
        return numpy.pi

    result = k8s.wait(k8s.run(pi), timeout=30)

    import numpy
    assert(result == numpy.pi)


def test_deps():
    job1 = k8s.run(lambda: "job-1")
    job2 = k8s.run(lambda result=k8s.wait(job1): "job-2 " + result) 
    result = k8s.wait(job2, timeout=30)
    assert(result == "job-2 job-1")


def test_simple_workflow():
    def wf():
        jobs1 = k8s.map(lambda i: i, range(3), nowait=True)
        return k8s.wait(k8s.run(lambda inputs: sum(inputs), map(k8s.wait, jobs1)), timeout=30)
    result = k8s.wait(k8s.run(wf), timeout=60)
    assert(result == 3)


def test_nested_lambda():
    job = k8s.run(lambda i, j: 10 * k8s.wait(k8s.run(lambda a=i, b=j: a+b)), 1,2)
    result = k8s.wait(job)
    assert(result == 30)


def test_ngs_workflow():
    from example_workflow import ngs_workflow
    results = ngs_workflow("batch-1")
    assert(results.__class__ == dict)


## Volumes

def test_volumes():
    def wf():
        import json
        volume = k8s.create_volume("10Mi")

        def func1():
            with open(f"/mnt/{volume}/test.json", "w") as fp:
                json.dump("hey", fp)

        def func2():
            with open(f"/mnt/{volume}/test.json") as fp:
                return json.load(fp)

        k8s.wait(k8s.run(func1, volumes=[volume]))
        result = k8s.wait(k8s.run(func2, volumes=[volume]))
                                  
        return result

    result = wf()
    assert(result == "hey")


def test_rwx_volumes():
    import time

    def wf():
        volume = k8s.create_volume("1Mi")

        def waiter():
            import json, os, time
            iteration_count = 0
            while True:
                if os.path.exists(f"/mnt/{volume}/test.json"):
                    with open(f"/mnt/{volume}/test.json") as fp:
                        data = json.load(fp)
                        return dict(iteration_count=iteration_count, data=data)
                time.sleep(1)
                iteration_count += 1

        def writer():
            import json, os
            with open(f"/mnt/{volume}/test.json", "w") as fp:
                json.dump("the writer has writ", fp)
            return True

        job1 = k8s.run(waiter, volumes=[volume])
        time.sleep(5)
        job2 = k8s.run(writer, volumes=[volume])
        k8s.wait(job2)
        result = k8s.wait(job1)
        return result

    result = wf()
    assert((result["iteration_count"] >= 0) and (result["data"] == "the writer has writ"))


# containers

def test_containers():
    image = k8s.docker_build("pysam", ancestor="jobs", conda=["pysam"], channels=["bioconda"], push=False)

    def test_pysam():
        import pysam
        return pysam.__file__

    result = k8s.wait(k8s.run(test_pysam, image=image))
    assert(result.__class__ == str)


# Resources

def test_resource_limits():
    import os
    def allocate_memory(size):
        import numpy
        numpy.random.bytes(size * int(1e6))
        return True

    assert(k8s.run(allocate_memory, 3, requests={"memory": "100Mi", "cpu": 1}, limits={"memory":"100Mi"}, nowait=False, timeout=20))

    try:
        job = k8s.run(allocate_memory, 1000, requests={"memory": "6Mi", "cpu": 1}, limits={"memory":"6Mi"})
        k8s.wait(job)
    except RuntimeError:
        assert(os.system(f"kubectl delete job {job}") == 0)
        return

    assert(False)


# Workflow State

# Test that we can actually spin up and shut down a MongoDB service
def test_mongodb():
    # Create a container that has pymongo installed
    image = k8s.docker_build("pymongo", ancestor="jobs", pip=["pymongo"], push=False)
    db = k8s.create_mongo_db()

    def insert_document(data, url=db["url"]):
        import pymongo
        client = pymongo.MongoClient(url)
        client.state.state.insert_one(data)
        return None

    def retrieve_document(query, url=db["url"]):
        import pymongo
        import json
        import bson.json_util
        client = pymongo.MongoClient(url)
        result = json.loads(bson.json_util.dumps(client.state.state.find_one(query)))  # little hack to work around serializing MongoDB ObjectId's
        return result

    result1 = k8s.run(insert_document, dict(hello="world", payload=42), image=image, nowait=False)
    result2 = k8s.run(retrieve_document, dict(hello="world"), image=image, nowait=False)

    print(result1)
    print(result2)

    k8s.delete_mongo_db(db)

    assert(result2.__class__ == dict)
    assert(result2["payload"] == 42)


# This is a test of our WorkflowState class:
def test_workflowstate():
    wfs = k8s.WorkflowState()

    def wf(wfs=wfs):
        answers = []

        wfs["foo"] = "bar"
        answers.append(wfs["foo"])

        wfs["fnord"] = "dronf"
        answers.append(wfs["fnord"])

        wfs["foo"] = "baz"
        answers.append(wfs["foo"])

        return answers
    
    answers = k8s.run(wf, nowait=False)
    assert(tuple(answers) == ("bar", "dronf", "baz"))


# Put all the workflow state together:
def test_stateful_workflow():
    image = k8s.docker_build("numpy", ancestor="jobs", pip=["numpy"], push=False)

    with k8s.WorkflowState() as state:
        def wf1():
            def func():
                import numpy
                return numpy.random.random()
            return k8s.run(func, state=state, timeout=30, nowait=False)  # Pass state as a parameter to run to benefit from memoization.

        a = k8s.run(wf1, nowait=False, image=image)
        b = k8s.run(wf1, nowait=False, image=image)

        def wf2():
            def func():
                import numpy
                return numpy.random.random()
            return k8s.run(func, timeout=30, nowait=False)

        c = k8s.run(wf2, nowait=False, image=image)
        d = k8s.run(wf2, nowait=False, image=image)

    assert(a == b)
    assert(c != d)
    assert(c != a)
    assert(d != a)


# WorkflowState can also be used as a hash table, globally available to all jobs, backed by a MongoDB
def test_freeform_state():
    with k8s.WorkflowState() as state:
        def wf1():

            def leave_message():
                state["message"] = "Hello from the trenches!"

            def get_message():
                return state["message"]

            k8s.run(leave_message, nowait=False, timeout=30)
            message = k8s.run(get_message, nowait=False, timeout=30)
            return message

        message = k8s.run(wf1, nowait=False)
        state.enter_local_mode()
        assert(state["message"] == "Hello from the trenches!")

    assert(message == "Hello from the trenches!")


# Module Config Files (WIP)
def test_configs():
    return None