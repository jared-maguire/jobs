test:
	pytest

local_config:
	python -m sk8s config-cluster -apply
	python -m sk8s config-local
	python -m sk8s containers
