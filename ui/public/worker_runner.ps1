# worker_runner.ps1 — AI-OS distributed worker (Windows)
# Instalar em C:\aios\worker_runner.ps1
# Correr: powershell -ExecutionPolicy Bypass -File C:\aios\worker_runner.ps1

$Server   = "http://100.121.255.36:3000"
$WorkerId = "DESKTOP-LPTDD99-agent"

# Token gerado em 2026-03-04 — guarda fora do script se possível:
#   setx AIOS_WORKER_TOKEN "d5fbc08f66dc1d1e3bdb83777c09a89091c1ae4d94e4968769f887db3bcebdf4"
$WorkerToken = if ($env:AIOS_WORKER_TOKEN) { $env:AIOS_WORKER_TOKEN } `
               else { "d5fbc08f66dc1d1e3bdb83777c09a89091c1ae4d94e4968769f887db3bcebdf4" }

$Headers = @{
    "X-AIOS-WORKER-ID"    = $WorkerId
    "X-AIOS-WORKER-TOKEN" = $WorkerToken
}

Write-Host "[worker] $WorkerId iniciado -> $Server"

while ($true) {
    try {
        # Heartbeat (sem token — endpoint público)
        Invoke-WebRequest -UseBasicParsing "$Server/api/workers/register?id=$WorkerId&hostname=$env:COMPUTERNAME&role=agent" -ErrorAction SilentlyContinue | Out-Null

        # Lease: obter próximo job
        $resp  = Invoke-WebRequest -UseBasicParsing -Headers $Headers "$Server/api/worker_jobs/lease?worker_id=$WorkerId"
        $lease = $resp.Content | ConvertFrom-Json

        if ($lease.ok -and $lease.job) {
            $job  = $lease.job
            $jid  = $job.id
            $kind = $job.kind
            $pl   = $job.payload

            Write-Host "[worker] job $jid kind=$kind"

            $status = "done"
            $result = @{ ok = $true; output = "" }

            try {
                if ($kind -eq "safe_action") {
                    $action = $pl.action
                    if ($action -eq "healthcheck") {
                        $health = Invoke-WebRequest -UseBasicParsing "$Server/api/syshealth" -ErrorAction Stop
                        $result = @{ ok = $true; output = "healthcheck OK"; status_code = $health.StatusCode }
                    } elseif ($action -eq "ping") {
                        $result = @{ ok = $true; output = "pong"; host = $env:COMPUTERNAME }
                    } else {
                        $result = @{ ok = $false; output = "unknown action: $action" }
                        $status = "failed"
                    }
                } elseif ($kind -eq "powershell") {
                    $cmd = $pl.command
                    if (-not $cmd) { throw "missing command" }
                    $out = Invoke-Expression $cmd 2>&1 | Out-String
                    $result = @{ ok = $true; output = $out.Substring(0, [Math]::Min(500, $out.Length)) }
                } else {
                    $result = @{ ok = $false; output = "unknown kind: $kind" }
                    $status = "failed"
                }
            } catch {
                $result = @{ ok = $false; output = $_.Exception.Message }
                $status = "failed"
            }

            # Report resultado
            $body = @{
                job_id = $jid
                status = $status
                result = $result
            } | ConvertTo-Json -Depth 5

            Invoke-WebRequest -UseBasicParsing -Headers $Headers `
                -Method POST "$Server/api/worker_jobs/report" `
                -ContentType "application/json" -Body $body | Out-Null

            Write-Host "[worker] job $jid -> $status"
        }
    } catch {
        Write-Host "[worker] erro: $($_.Exception.Message)"
    }

    Start-Sleep -Seconds 15
}
