# setup_win_portproxy.ps1
# Run ONCE in an elevated (admin) PowerShell on the Windows host.
# Adds Windows portproxy rules so cluster nodes (192.168.1.x) can reach
# the AI-OS services running inside WSL2.
#
# Usage: Right-click PowerShell -> "Run as Administrator", then:
#   .\setup_win_portproxy.ps1

$WSL_IP = "172.22.158.152"  # Update if WSL2 IP changes

$rules = @(
    @{ Port = 5432;  Desc = "Postgres"    },
    @{ Port = 8010;  Desc = "agent-core"  },
    @{ Port = 1883;  Desc = "MQTT"        },
    @{ Port = 3000;  Desc = "UI Express"  },
    @{ Port = 8000;  Desc = "status API"  }
)

foreach ($r in $rules) {
    $port = $r.Port
    $desc = $r.Desc
    Write-Host "Adding portproxy for $desc (:$port -> WSL2:$port)"
    netsh interface portproxy add v4tov4 `
        listenaddress=0.0.0.0 `
        listenport=$port `
        connectaddress=$WSL_IP `
        connectport=$port
}

Write-Host ""
Write-Host "Current portproxy rules:"
netsh interface portproxy show all

Write-Host ""
Write-Host "Adding Windows Firewall rules (if missing)..."
foreach ($r in $rules) {
    $port = $r.Port
    New-NetFirewallRule -DisplayName "AIOS $($r.Desc) $port" `
        -Direction Inbound -Protocol TCP -LocalPort $port `
        -Action Allow -ErrorAction SilentlyContinue | Out-Null
}

Write-Host "Done. Cluster nodes can now reach 192.168.1.101:<port>"
Write-Host ""
Write-Host "To verify from a cluster node:"
Write-Host "  nc -zv 192.168.1.101 5432 && echo OK"
