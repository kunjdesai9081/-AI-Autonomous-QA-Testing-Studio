param([Parameter(ValueFromRemainingArguments = $true)]$argsList)
$py = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }
& $py -m streamlit @argsList
