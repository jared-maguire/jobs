# sk8s - Run Python Functions on Kubernetes

sk8s (pronounced "skates") is a Python library that makes it easy to run Python functions as Kubernetes jobs. It enables distributed computing workflows with minimal boilerplate, allowing you to focus on your code rather than Kubernetes configuration.

## Key Features

- **Simple API**: Run Python functions on Kubernetes with just `sk8s.run()`
- **Distributed Workflows**: Jobs can submit other jobs, enabling complex DAG workflows
- **Automatic Serialization**: Functions and arguments are automatically serialized using `dill`
- **Result Collection**: Results are automatically collected from completed jobs
- **Volume Support**: Easy data sharing between jobs using persistent volumes
- **Custom Containers**: Build and use Docker images with specific dependencies
- **Resource Management**: Configure CPU, memory, and GPU resources per job
- **Cloud Support**: Works with local clusters, GKE, EKS, and more
- **Object Storage**: Results can be persisted to S3 (new in latest version)

## Quick Start

```python
import sk8s

# Run a simple function
result = sk8s.run(lambda x: x * 2, 5, asynchro=False)
print(result)  # Output: 10

# Run functions in parallel
results = sk8s.map(lambda x: x ** 2, range(5))
print(results)  # Output: [0, 1, 4, 9, 16]

# Create a workflow
def workflow():
    # Step 1: Process data in parallel
    squared = sk8s.map(lambda x: x ** 2, range(5))
    
    # Step 2: Sum the results
    total = sk8s.run(lambda vals: sum(vals), squared, asynchro=False)
    
    return total

result = sk8s.run(workflow, asynchro=False)
print(result)  # Output: 30
```

## Core API

### sk8s.run(function, *args, **kwargs)

Submit a Python function to run as a Kubernetes job.

**Parameters:**
- `function`: The Python function to execute
- `*args`: Arguments to pass to the function
- `asynchro` (bool, default=True): If True, return job name immediately. If False, wait for completion and return result
- `timeout` (int): Timeout in seconds when asynchro=False
- `image` (str): Docker image to use (defaults to configured image)
- `volumes` (dict/list): Volumes to mount in the pod
- `requests` (dict): Resource requests (e.g., `{"cpu": "1", "memory": "1Gi"}`)
- `limits` (dict): Resource limits
- `backoffLimit` (int, default=0): Number of retries for failed jobs
- `name` (str): Job name template (e.g., `"myjob-{s}"`)

**Returns:**
- If asynchro=True: Job name (string)
- If asynchro=False: Function result

### sk8s.wait(jobs, timeout=None, delete=True)

Wait for job(s) to complete and return results.

**Parameters:**
- `jobs` (str/list): Job name or list of job names
- `timeout` (int): Maximum wait time in seconds
- `delete` (bool): Delete jobs after completion

**Returns:**
- Single result if one job, list of results if multiple jobs

### sk8s.map(function, iterable, **kwargs)

Apply a function to each element of an iterable in parallel.

**Parameters:**
- `function`: Function to apply
- `iterable`: Input data
- All parameters from `sk8s.run()` are supported

**Returns:**
- List of results in the same order as inputs

### sk8s.starmap(function, iterable, **kwargs)

Like `map()` but unpacks arguments from tuples.

```python
# Example: starmap unpacks tuples as arguments
results = sk8s.starmap(lambda x, y: x + y, [(1, 2), (3, 4), (5, 6)])
# Results: [3, 7, 11]
```

## Volume Management

### Creating and Using Volumes

```python
import sk8s
import json

# Create a volume
volume = sk8s.create_volume("1Gi", accessModes=["ReadWriteMany"])

# Write data in one job
def writer():
    data = {"message": "Hello from sk8s!", "count": 42}
    with open(f"/mnt/{volume}/data.json", "w") as f:
        json.dump(data, f)
    return "Data written"

# Read data in another job
def reader():
    with open(f"/mnt/{volume}/data.json", "r") as f:
        return json.load(f)

# Execute the workflow
sk8s.run(writer, volumes=[volume], asynchro=False)
result = sk8s.run(reader, volumes=[volume], asynchro=False)
print(result)  # Output: {'message': 'Hello from sk8s!', 'count': 42}

# Clean up
sk8s.delete_volume(volume)
```

## Advanced Examples

### Complex Workflow with Dependencies

```python
import sk8s

def data_processing_pipeline(data_urls):
    """A complex data processing pipeline"""
    
    # Stage 1: Download and preprocess data in parallel
    def download_and_preprocess(url):
        import requests
        import pandas as pd
        # Simulate downloading and preprocessing
        data = requests.get(url).json()
        df = pd.DataFrame(data)
        return df.describe().to_dict()
    
    preprocessed = sk8s.map(download_and_preprocess, data_urls)
    
    # Stage 2: Analyze each dataset
    def analyze(data_stats):
        # Perform analysis on statistics
        total_count = sum(stats.get('count', {}).get('count', 0) for stats in data_stats)
        return {"total_records": total_count, "datasets": len(data_stats)}
    
    analysis = sk8s.run(analyze, preprocessed, asynchro=False)
    
    # Stage 3: Generate final report
    def generate_report(analysis_result):
        report = f"Processed {analysis_result['datasets']} datasets\n"
        report += f"Total records: {analysis_result['total_records']}"
        return report
    
    final_report = sk8s.run(generate_report, analysis, asynchro=False)
    return final_report

# Run the pipeline
urls = ["http://api1.example.com/data", "http://api2.example.com/data"]
result = sk8s.run(data_processing_pipeline, urls, asynchro=False)
```

### Using Custom Docker Images

```python
import sk8s

# Build a custom image with scientific libraries
scientific_image = sk8s.docker_build(
    "scientific-compute",
    conda=["numpy", "scipy", "scikit-learn", "pandas"],
    channels=["conda-forge"]
)

# Use the custom image for computation
def scientific_computation():
    import numpy as np
    from sklearn.datasets import make_classification
    from sklearn.model_selection import train_test_split
    from sklearn.ensemble import RandomForestClassifier
    
    # Generate synthetic data
    X, y = make_classification(n_samples=1000, n_features=20)
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2)
    
    # Train model
    clf = RandomForestClassifier(n_estimators=100)
    clf.fit(X_train, y_train)
    
    # Return accuracy
    return clf.score(X_test, y_test)

accuracy = sk8s.run(scientific_computation, image=scientific_image, asynchro=False)
print(f"Model accuracy: {accuracy}")
```

### Resource Management and GPU Support

```python
# CPU and memory intensive task
result = sk8s.run(
    lambda: "Memory intensive computation",
    requests={"memory": "4Gi", "cpu": "2"},
    limits={"memory": "8Gi", "cpu": "4"},
    asynchro=False
)

# GPU task (requires GPU nodes in cluster)
gpu_result = sk8s.run(
    lambda: "GPU computation",
    requests={"nvidia.com/gpu": "1"},
    limits={"nvidia.com/gpu": "1"},
    asynchro=False
)
```

### Error Handling and Retries

```python
def flaky_function():
    import random
    if random.random() < 0.5:
        raise Exception("Random failure")
    return "Success!"

# Run with retries
result = sk8s.run(
    flaky_function,
    backoffLimit=3,  # Retry up to 3 times
    asynchro=False
)
```

## Configuration

### Configuration Structure

sk8s uses a JSON configuration file stored at `~/.sk8s/config.json`. Key configuration options:

```json
{
    "docker_image_prefix": "localhost:5000/",
    "docker_default_pull_policy": "Always",
    "default_storageclass": "standard",
    "service_account_name": "sk8s",
    "result_obs_prefix": "s3://my-bucket/sk8s-results/"
}
```

### Result Storage with Object Storage (S3)

The latest version supports storing job results in S3 instead of retrieving them from pod logs:

```python
# Configure S3 result storage
import sk8s.configs

config = sk8s.configs.load_config()
config["result_obs_prefix"] = "s3://my-bucket/sk8s-results/"
sk8s.configs.save_config(config)

# Results will now be automatically stored in S3
result = sk8s.run(lambda: {"data": "important result"}, asynchro=False)
```

# Installation

## Prerequisites

- Python 3.8+
- Access to a Kubernetes cluster (local or cloud)
- `kubectl` configured to access your cluster
- Docker (for building custom images)

## Local Cluster (Docker Desktop)

```bash
# Clone the repository
git clone https://github.com/jared-maguire/jobs.git
cd jobs

# Create and activate a virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install sk8s
pip install -e .

# Configure the cluster (creates service account and permissions)
python -m sk8s config-cluster -apply

# Verify installation
python -c "import sk8s; print(sk8s.run(lambda: 'Hello from k8s!', asynchro=False))"
```

## Google Kubernetes Engine (GKE)

1. Create a GKE cluster with Filestore CSI driver enabled
2. Configure `kubectl` and `docker` for GKE:

```bash
gcloud container clusters get-credentials YOUR_CLUSTER --zone YOUR_ZONE
gcloud auth configure-docker
```

3. Configure sk8s:

```bash
python -m sk8s config-gke -project PROJECT_NAME -namespace NAMESPACE
python -m sk8s containers -push
```

## Amazon Elastic Kubernetes Service (EKS)

1. Create an EKS cluster and ECR repository
2. Configure `kubectl` and `docker` for EKS:

```bash
aws eks update-kubeconfig --name YOUR_CLUSTER
aws ecr get-login-password --region REGION | docker login --username AWS --password-stdin ACCOUNT.dkr.ecr.REGION.amazonaws.com
```

3. Configure sk8s:

```bash
python -m sk8s config-eks -account ACCOUNT_ID -region REGION -namespace NAMESPACE -result_prefix s3://YOUR_BUCKET/results/
python -m sk8s containers -push
```

## Troubleshooting

### Common Issues

1. **"No service account found"**: Run `python -m sk8s config-cluster -apply`
2. **"Image pull errors"**: Ensure your Docker registry is accessible from the cluster
3. **"Job failed"**: Check job logs with `kubectl logs job/JOB_NAME`
4. **"Volume access denied"**: Verify your storage class supports the requested access mode

### Debugging Jobs

```python
# Get job status
job = sk8s.run(lambda: "test")
# Check with kubectl: kubectl describe job/job-xxxxx

# Keep failed jobs for debugging (don't auto-delete)
try:
    result = sk8s.wait(job, delete=False)
except RuntimeError:
    # Examine the job: kubectl logs job/job-xxxxx
    pass
```

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the LICENSE file for details.