# Run as Administrator - fixes portproxy after WSL2 reboot
$wsl_ip = (wsl hostname -I).Trim().Split()[0]
Write-Host "WSL2 IP: $wsl_ip"

# Remove old rules
netsh interface portproxy delete v4tov4 listenport=3000 listenaddress=0.0.0.0 2>$null
netsh interface portproxy delete v4tov4 listenport=5432 listenaddress=0.0.0.0 2>$null
netsh interface portproxy delete v4tov4 listenport=8010 listenaddress=0.0.0.0 2>$null

# Add updated rules with current WSL2 IP
netsh interface portproxy add v4tov4 listenport=3000 listenaddress=0.0.0.0 connectport=3000 connectaddress=$wsl_ip
netsh interface portproxy add v4tov4 listenport=5432 listenaddress=0.0.0.0 connectport=5432 connectaddress=$wsl_ip
netsh interface portproxy add v4tov4 listenport=8010 listenaddress=0.0.0.0 connectport=8010 connectaddress=$wsl_ip

# Ensure firewall rule exists for port 3000
netsh advfirewall firewall delete rule name="AIOS UI 3000" 2>$null
netsh advfirewall firewall add rule name="AIOS UI 3000" dir=in action=allow protocol=TCP localport=3000

netsh interface portproxy show all
Write-Host "Done."
