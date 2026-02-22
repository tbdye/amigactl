$env:PYTHONPATH = Join-Path $PSScriptRoot "client"
python -m amigactl @args
