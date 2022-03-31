#!/bin/bash -e

docker build -f docker/Dockerfile -t humusnet-container . $@
