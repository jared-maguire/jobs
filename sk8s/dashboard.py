#!/usr/bin/env python3

import sk8s

import curses
from kubernetes import client, config
from time import sleep
from collections import Counter

def get_k8s_resources(namespace):
    config.load_kube_config()
    v1 = client.CoreV1Api()
    batch_v1 = client.BatchV1Api()
    apps_v1 = client.AppsV1Api()

    # Get pods, jobs, persistent volumes, deployments, and services in the namespace
    pods = v1.list_namespaced_pod(namespace=namespace).items
    jobs = batch_v1.list_namespaced_job(namespace=namespace).items
    volumes = v1.list_persistent_volume_claim_for_all_namespaces().items
    deployments = apps_v1.list_namespaced_deployment(namespace=namespace).items
    services = v1.list_namespaced_service(namespace=namespace).items

    # Stratify pods and jobs by status
    pod_statuses = Counter([pod.status.phase for pod in pods])
    job_statuses = Counter([job.status.conditions[0].type if job.status.conditions else "Unknown" for job in jobs])

    # Get volumes with sizes
    volume_sizes = {vol.metadata.name: vol.status.capacity.get("storage", "Unknown") for vol in volumes if vol.metadata.namespace == namespace}

    # Get deployment and service names
    deployment_names = [dep.metadata.name for dep in deployments]
    service_names = [svc.metadata.name for svc in services]

    return pod_statuses, job_statuses, volume_sizes, deployment_names, service_names

def init_colors(light_theme=True):
    curses.start_color()
    if light_theme:
        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_WHITE)  # Background
        curses.init_pair(2, curses.COLOR_BLUE, curses.COLOR_WHITE)    # Titles
        curses.init_pair(3, curses.COLOR_BLACK, curses.COLOR_WHITE)   # Text
    else:
        curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLACK)  # Background
        curses.init_pair(2, curses.COLOR_CYAN, curses.COLOR_BLACK)   # Titles
        curses.init_pair(3, curses.COLOR_WHITE, curses.COLOR_BLACK)  # Text

def live_dashboard(stdscr, namespace="default", refresh_rate=2):
    curses.curs_set(0)
    stdscr.nodelay(1)
    stdscr.clear()

    # Set the default theme to light
    light_theme = False
    init_colors(light_theme)

    while True:
        stdscr.clear()
        
        # Set colors based on theme
        background = curses.color_pair(1)
        title_color = curses.color_pair(2)
        text_color = curses.color_pair(3)

        pod_statuses, job_statuses, volume_sizes, deployment_names, service_names = get_k8s_resources(namespace)

        #stdscr.bkgd(' ', background)
        stdscr.addstr(0, 0, f"Kubernetes Dashboard - Namespace: {namespace}", title_color | curses.A_BOLD)

        # Get the current terminal width and height
        height, width = stdscr.getmaxyx()
        stdscr.addstr(1, 0, "-" * width, title_color)

        # Aggregate all statuses for columns
        all_statuses = set(pod_statuses.keys()).union(job_statuses.keys())

        # Display Table Header
        stdscr.addstr(3, 0, f"{'Resource':<10}", title_color | curses.A_BOLD)
        col = 10
        for status in all_statuses:
            stdscr.addstr(3, col, f"{status:<15}", title_color | curses.A_BOLD)
            col += 15
        stdscr.addstr(4, 0, "-" * width, title_color)

        # Display Pod Counts by Status
        line = 5
        stdscr.addstr(line, 0, f"{'Pod':<10}", text_color | curses.A_BOLD)
        col = 10
        for status in all_statuses:
            count = pod_statuses.get(status, 0)
            stdscr.addstr(line, col, f"{count:<15}", text_color)
            col += 15
        line += 1

        # Display Job Counts by Status
        stdscr.addstr(line, 0, f"{'Job':<10}", text_color | curses.A_BOLD)
        col = 10
        for status in all_statuses:
            count = job_statuses.get(status, 0)
            stdscr.addstr(line, col, f"{count:<15}", text_color)
            col += 15
        line += 2

        # Display Volume Information
        stdscr.addstr(line + 1, 0, "Volumes and Sizes:", title_color | curses.A_BOLD)
        line += 2
        for vol_name, size in volume_sizes.items():
            stdscr.addstr(line, 2, f"{vol_name}: {size}", text_color)
            line += 1

        # Display Deployment Names
        stdscr.addstr(line + 1, 0, "Deployments:", title_color | curses.A_BOLD)
        line += 2
        for dep_name in deployment_names:
            stdscr.addstr(line, 2, dep_name, text_color)
            line += 1

        # Display Service Names
        stdscr.addstr(line + 1, 0, "Services:", title_color | curses.A_BOLD)
        line += 2
        for svc_name in service_names:
            stdscr.addstr(line, 2, svc_name, text_color)
            line += 1

        # Refresh screen
        stdscr.refresh()

        # Check for key inputs to toggle theme or quit
        key = stdscr.getch()
        if key == ord('q'):
            break
        elif key == ord('t'):  # Press 't' to toggle theme
            light_theme = not light_theme
            init_colors(light_theme)
        
        # Wait for the specified refresh rate
        sleep(refresh_rate)


def show_dashboard():
    curses.wrapper(live_dashboard, sk8s.get_current_namespace())
