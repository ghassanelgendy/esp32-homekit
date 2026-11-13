# windows_agent_setup.ps1 - Windows PowerShell Background Agent
# Run on Windows PC / Laptop with administrative PowerShell or as a scheduled task.
param (
    [string]$DeviceName = "windows_pc",
    [string]$BrokerIp = "192.168.1.100",
    [int]$Port = 1883
)

Write-Host "Starting Power Control Agent for $DeviceName connecting to $BrokerIp..." -ForegroundColor Cyan

# Check if Python and paho-mqtt are available
if (Get-Command python -ErrorAction SilentlyContinue) {
    Write-Host "Python detected. Ensuring paho-mqtt is installed..."
    python -m pip install --quiet paho-mqtt
    $scriptContent = @'
import sys, time, subprocess
import paho.mqtt.client as mqtt

BROKER_IP = sys.argv[1]
DEVICE_NAME = sys.argv[2]
TOPIC_AVAILABILITY = f"hassagent/{DEVICE_NAME}/availability"
TOPIC_CMD_SHUTDOWN = f"hassagent/{DEVICE_NAME}/command/shutdown"
TOPIC_CMD_RESTART = f"hassagent/{DEVICE_NAME}/command/restart"

def on_connect(c, u, f, rc):
    c.publish(TOPIC_AVAILABILITY, "online", retain=True)
    c.subscribe(TOPIC_CMD_SHUTDOWN)
    c.subscribe(TOPIC_CMD_RESTART)
    print(f"Connected to {BROKER_IP}. Ready.")

def on_message(c, u, msg):
    if msg.topic == TOPIC_CMD_SHUTDOWN:
        c.publish(TOPIC_AVAILABILITY, "offline", retain=True)
        time.sleep(1)
        subprocess.run(["shutdown", "/s", "/t", "0"])
    elif msg.topic == TOPIC_CMD_RESTART:
        c.publish(TOPIC_AVAILABILITY, "offline", retain=True)
        time.sleep(1)
        subprocess.run(["shutdown", "/r", "/t", "0"])

c = mqtt.Client(client_id=f"win_agent_{DEVICE_NAME}")
c.will_set(TOPIC_AVAILABILITY, "offline", retain=True)
c.on_connect = on_connect
c.on_message = on_message
c.connect(BROKER_IP, 1883, 60)
c.loop_forever()
'@
    $tempPy = "$env:TEMP\win_mqtt_agent.py"
    Set-Content -Path $tempPy -Value $scriptContent -Encoding UTF8
    python $tempPy $BrokerIp $DeviceName
} else {
    Write-Host "Please install Python or HASS.Agent on this machine." -ForegroundColor Yellow
}
