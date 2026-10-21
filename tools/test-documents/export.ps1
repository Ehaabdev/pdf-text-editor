# Saves every .docx in backend/tests/fixtures/word as PDF using Microsoft Word,
# exactly like File > Save As > PDF. Output: <name>-word.pdf next to each .docx.
# Run after `node make.js`. Needs Microsoft Word on Windows; close Word first.
#   powershell -File export.ps1                    all documents
#   powershell -File export.ps1 french-quote       just one
#
# Each document is converted by a fresh Word in its own background job. Driving
# Word directly from this script sometimes hangs forever while saving the PDF;
# in a job it doesn't, and a stuck job can be timed out and cleaned up.

$dir = (Resolve-Path (Join-Path $PSScriptRoot "..\..\backend\tests\fixtures\word")).Path
$only = $args
$timeoutSeconds = 60

if (Get-Process WINWORD -ErrorAction SilentlyContinue) {
    Write-Error "Word is open. Close it first so this script doesn't interfere with your documents."
    exit 1
}

$convert = {
    param($src, $dst)
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0          # wdAlertsNone
    $word.AutomationSecurity = 3     # never run or ask about macros
    $doc = $word.Documents.Open($src, $false, $true, $false)
    $doc.SaveAs2($dst, 17)           # 17 = wdFormatPDF
    $doc.Close(0)                    # wdDoNotSaveChanges
    $word.Quit(0)
}

$failed = 0
foreach ($file in Get-ChildItem $dir -Filter *.docx) {
    if ($only -and -not ($only -contains $file.BaseName)) { continue }
    $pdf = Join-Path $dir ($file.BaseName + "-word.pdf")
    $job = Start-Job -ScriptBlock $convert -ArgumentList $file.FullName, $pdf
    $null = Wait-Job $job -Timeout $timeoutSeconds
    if ($job.State -eq "Completed") {
        Write-Output "saved $($file.BaseName)-word.pdf"
    } else {
        Write-Output "FAILED $($file.Name): $($job.State)"
        $failed++
    }
    # A stuck Word blocks its job; ending Word lets the job finish.
    Get-Process WINWORD -ErrorAction SilentlyContinue | Stop-Process -Force
    $null = Wait-Job $job -Timeout 10
    Remove-Job $job -Force -ErrorAction SilentlyContinue
}
exit $failed
