#!/usr/bin/env python
import pytest
import sk8s


## Cluster

@pytest.mark.cluster
def test_cluster_config():
    assert(sk8s.check_cluster_config())


## Jobs

@pytest.mark.jobs
def test_run_and_wait():
    result = sk8s.wait(sk8s.run(lambda: "Hooray"), timeout=500)
    assert(result=="Hooray")


@pytest.mark.jobs
def test_run_and_wait_2():
    result = sk8s.run(lambda: "Hooray", asynchro=False, timeout=500)
    assert(result=="Hooray")


@pytest.mark.jobs
def test_fail():
    import os
    try:
        job = sk8s.run(lambda: 1/0)
        job = sk8s.wait(job)
    except RuntimeError:
        assert(os.system(f"kubectl delete job {job}") == 0)
        return
    assert(False)


@pytest.mark.jobs
def test_map():
    results = sk8s.map(lambda i: i*2, (0,1,2))
    assert(tuple(results) == (0,2,4))


@pytest.mark.jobs
def test_imports():
    def pi():
        import numpy
        return numpy.pi

    result = sk8s.wait(sk8s.run(pi), timeout=500)

    import numpy
    assert(result == numpy.pi)


@pytest.mark.jobs
def test_deps():
    job1 = sk8s.run(lambda: "job-1")
    job2 = sk8s.run(lambda result=sk8s.wait(job1): "job-2 " + result) 
    result = sk8s.wait(job2, timeout=500)
    assert(result == "job-2 job-1")


@pytest.mark.jobs
def test_simple_workflow():
    def wf():
        jobs1 = sk8s.map(lambda i: i, range(3), asynchro=True)
        return sk8s.wait(sk8s.run(lambda inputs: sum(inputs),
                                  map(sk8s.wait, jobs1),
                                  name="job2-{s}",
                                  asynchro=True),
                         timeout=500)
    result = sk8s.wait(sk8s.run(wf, name="wf-{s}"), timeout=500)
    assert(result == 3)


@pytest.mark.jobs
def test_nested_lambda():
    job = sk8s.run(lambda i, j: 10 * sk8s.wait(sk8s.run(lambda a=i, b=j: a+b)), 1,2)
    result = sk8s.wait(job)
    assert(result == 30)


#def test_ngs_workflow():
#    from example_workflow import ngs_workflow
#    results = ngs_workflow("batch-1")
#    assert(results.__class__ == dict)


## Volumes

@pytest.mark.volumes
def test_volumes():
    def wf():
        import json
        #volume = sk8s.create_volume("10Mi", accessModes=["ReadOnlyMany"])
        volume = sk8s.create_volume("10Mi", accessModes=["ReadWriteOnce"])

        def func1():
            with open(f"/mnt/{volume}/test.json", "w") as fp:
                json.dump("hey", fp)

        def func2():
            with open(f"/mnt/{volume}/test.json") as fp:
                return json.load(fp)

        sk8s.wait(sk8s.run(func1, volumes=[volume]))
        result = sk8s.wait(sk8s.run(func2, volumes=[volume]))

        sk8s.delete_volume(volume)
                                  
        return result

    result = wf()
    assert(result == "hey")


@pytest.mark.volumes
def test_rwx_volumes():
    import time

    def wf():
        volume = sk8s.create_volume("1Mi")

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

        job1 = sk8s.run(waiter, volumes=[volume])
        time.sleep(5)

        job2 = sk8s.run(writer, volumes=[volume])

        sk8s.wait(job2, timeout=60*5)
        result = sk8s.wait(job1, timeout=60*5)
        sk8s.delete_volume(volume)

        return result

    result = wf()
    assert((result["iteration_count"] >= 0) and (result["data"] == "the writer has writ"))


# containers

@pytest.mark.containers
def test_containers():
    image = sk8s.docker_build("pysam", conda=["pysam"], channels=["bioconda"])

    def test_pysam():
        import pysam  # type: ignore
        return pysam.__file__

    result = sk8s.wait(sk8s.run(test_pysam, image=image))
    assert(result.__class__ == str)


# Resources

@pytest.mark.jobs
def test_resource_limits():
    import os
    def allocate_memory(size):
        import numpy
        numpy.random.bytes(size * int(1e6))
        return True

    assert(sk8s.run(allocate_memory, 3, requests={"memory": "100Mi", "cpu": 1}, limits={"memory":"100Mi"}, asynchro=False, timeout=500))

    try:
        job = sk8s.run(allocate_memory, 1000, requests={"memory": "6Mi", "cpu": 1}, limits={"memory":"6Mi"})
        sk8s.wait(job)
    except RuntimeError:
        assert(os.system(f"kubectl delete job {job}") == 0)
        return

    assert(False)


# Workflow State

""" Disable WorkflowState for now...

# Test that we can actually spin up and shut down a MongoDB service
def test_mongodb():
    # Create a container that has pymongo installed
    image = sk8s.docker_build("pymongo", pip=["pymongo"])
    db = sk8s.create_mongo_db()

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

    result1 = sk8s.run(insert_document, dict(hello="world", payload=42), image=image, asynchro=False)
    result2 = sk8s.run(retrieve_document, dict(hello="world"), image=image, asynchro=False)

    print(result1)
    print(result2)

    sk8s.delete_mongo_db(db)

    assert(result2.__class__ == dict)
    assert(result2["payload"] == 42)


# This is a test of our WorkflowState class:
def test_workflowstate():
    wfs = sk8s.WorkflowState()

    def wf(wfs=wfs):
        answers = []

        wfs["foo"] = "bar"
        answers.append(wfs["foo"])

        wfs["fnord"] = "dronf"
        answers.append(wfs["fnord"])

        wfs["foo"] = "baz"
        answers.append(wfs["foo"])

        return answers
    
    answers = sk8s.run(wf, asynchro=False)
    assert(tuple(answers) == ("bar", "dronf", "baz"))


# Put all the workflow state together:
def test_stateful_workflow():
    image = sk8s.docker_build("numpy", pip=["numpy"])

    with sk8s.WorkflowState() as state:
        def wf1():
            def func():
                import numpy
                return numpy.random.random()
            return sk8s.run(func, state=state, timeout=500, asynchro=False)  # Pass state as a parameter to run to benefit from memoization.

        a = sk8s.run(wf1, asynchro=False, image=image)
        b = sk8s.run(wf1, asynchro=False, image=image)

        def wf2():
            def func():
                import numpy
                return numpy.random.random()
            return sk8s.run(func, timeout=500, asynchro=False)

        c = sk8s.run(wf2, asynchro=False, image=image)
        d = sk8s.run(wf2, asynchro=False, image=image)

    assert(a == b)
    assert(c != d)
    assert(c != a)
    assert(d != a)


# WorkflowState can also be used as a hash table, globally available to all jobs, backed by a MongoDB
def test_freeform_state():
    with sk8s.WorkflowState() as state:
        def wf1():

            def leave_message():
                state["message"] = "Hello from the trenches!"

            def get_message():
                return state["message"]

            sk8s.run(leave_message, asynchro=False, timeout=500)
            message = sk8s.run(get_message, asynchro=False, timeout=500)
            return message

        message = sk8s.run(wf1, asynchro=False)
        state.enter_local_mode()
        assert(state["message"] == "Hello from the trenches!")

    assert(message == "Hello from the trenches!")
"""


# Module Config Files

@pytest.mark.config
def test_configs():
    def test_config_job():
        import sk8s
        original = sk8s.load_config()
        new_config = original.copy()
        new_config["fnord"] = "dronf"
        if original == new_config:
            return "fail-1"
        sk8s.save_config(new_config)
        new_new_config = sk8s.load_config()
        if new_new_config != new_config:
            return "fail-2"
        return "pass"

    result = sk8s.run(test_config_job, asynchro=False)
    assert(result == "pass")