#!/bin/bash
cd "$(dirname "$0")"
echo "Installing the booth components..."
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
echo
echo "Done. Double-click \"RoarFun Booth.command\" to start."
read -p "Press return to close."
