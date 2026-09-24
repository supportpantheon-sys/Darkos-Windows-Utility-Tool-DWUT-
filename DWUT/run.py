"""
Entry point for running DWUT from source.
Usage: python run.py
"""
import sys
import os

# Add project root to path so all imports work
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.main import main
main()
