#!/bin/sh
W="C:/Users/sahil/Personal Data/VMR Data - Laptop/Outbound Agent/vmr-outbound-wt-campaign-url-offering"
M="c:/Users/sahil/Personal Data/VMR Data - Laptop/Outbound Agent/vmr-outbound"
export VMR_TEST_DATABASE_URL='postgresql+psycopg://postgres:dbPost#2026@localhost:5432/vmr_test_urlofr'
export PYTHONIOENCODING=utf-8
cd "$W" && exec "$M/.venv/Scripts/python.exe" -m pytest "$@"
