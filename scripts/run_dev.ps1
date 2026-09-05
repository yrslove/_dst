$uvicornArgs = @('app.main:app', '--host', '127.0.0.1', '--port', '8080', '--reload')
if (Test-Path -LiteralPath '.env') { $uvicornArgs += @('--env-file', '.env') }
python -m uvicorn @uvicornArgs
