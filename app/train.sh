#!/bin/bash --login

conda activate humusnet-env
export PYTHONPATH=.

$@
