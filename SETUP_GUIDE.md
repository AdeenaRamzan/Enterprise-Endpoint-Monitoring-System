# 🛠️ IT Deployment & Setup Guide

This guide provides step-by-step instructions for deploying the **CyberSentinel Central Command Backend** and building/distributing the **`employee_agent.exe`** standalone executable across your local network (LAN) or WAN.

---

## 📋 Prerequisites

### Backend Server Machine
* **Operating System**: Windows 10/11 or Windows Server (Linux supported).
* **Python Version**: Python 3.10+ installed.
* **Network**: Port `8000` accessible on local network (Windows Firewall rule required).

### Employee Target Machine
* **Operating System**: Windows 10 or Windows 11 (64-bit).
* **Permissions**: User privileges (Administrator optional, required only for registry startup persistence).

---

## 🖥️ Part 1: Central Command Server Setup

### Step 1: Open Firewall Port 8000 (Windows Server)
Run PowerShell as Administrator on the IT Server machine to allow incoming agent connections:
```powershell
netsh advfirewall firewall add rule name="CyberSentinel 8000" dir=in action=allow protocol=TCP localport=8000
```

### Step 2: Install Backend Dependencies
```bash
cd backend
pip install -r requirements.txt
```

### Step 3: Launch Central Server
```bash
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```
> Verify server health in your browser by visiting:  
> **`http://localhost:8000/health`** → `{"status":"ok"}`  
> Cyber Command Console: **`http://192.168.100.14:8000`** (replace with your server's IPv4 address).

---

## 💻 Part 2: Building & Deploying the Employee Executable

### Step 1: Configure Default Server IP
Open `agent/config.json` and set your server's IPv4 address:
```json
{
  "employee_id": "DESKTOP-EMPLOYEE",
  "backend_url": "http://192.168.100.14:8000",
  "screenshot_interval_seconds": 10,
  "delete_after_upload": true
}
```

### Step 2: Compile Standalone `.exe` with PyInstaller
Run the build script in the `agent` directory:
```bash
cd agent
python -m PyInstaller --clean --onefile --noconsole --add-data "monitoring_policy.txt;." --name employee_agent agent.py
```

Upon completion, your single executable binary will be generated at:
`agent/dist/employee_agent.exe`

### Step 3: Deploy Executable to Employee Machine
1. Copy `employee_agent.exe` to the target employee PC (via USB or network share).
2. Double-click **`employee_agent.exe`** to run.
3. Accept the **Employee Consent Agreement** dialog on first launch.
4. If the agent cannot reach the server URL, an interactive IT prompt will appear asking to confirm/enter the IT Server IP address.

---

## 🧪 Part 3: Verification & Test Walkthrough

1. Open your browser and navigate to **`http://192.168.100.14:8000`**.
2. **Overview Tab**: Verify total endpoints and active presence.
3. **Live Wall Tab**: Observe live screen feeds or black screen **`SIGNAL LOST // OFFLINE`** banners for disconnected machines.
4. **Remote Desktop Tab**: Click **Start Remote Control** to view and interactively control target screens with 20 FPS live video and DPI-aware mouse clicks.
5. **Alerts & Rules Tab**: Open `web.whatsapp.com` on an employee machine — observe the immediate **Warning Alert Event** logged in real time!

---

## 🔧 Troubleshooting

* **Agent fails to connect to backend**:
  - Run `ipconfig` on the IT Server PC to confirm its IPv4 address.
  - Test ping from employee PC: `ping 192.168.100.14`.
  - Ensure Windows Firewall rule for port `8000` is active.

* **Remote mouse clicks slightly offset**:
  - Ensure `employee_agent.exe` has been recompiled with the latest Win32 DPI-aware scaling fix (`0x8001`).
