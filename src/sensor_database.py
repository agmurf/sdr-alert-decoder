"""
ALERT Sensor Database - Parse and cache sensor information from sensors.xlsx
"""
import pandas as pd
import json
import sys
from pathlib import Path
from typing import Dict, Optional, Tuple

class SensorDatabase:
    def __init__(self, excel_path: str = "Sensors.xlsx"):
        """Initialize sensor database from Excel file"""
        self.excel_path = self._resolve(excel_path)
        self.sensors = {}
        self.errts_to_site = {}
        self.load_sensors()

    @staticmethod
    def _resolve(name: str) -> Path:
        """Find a data file regardless of the current working directory.

        The app is launched from anywhere (a shell in System32, a desktop
        shortcut, the frozen exe), so a bare relative name must be looked up
        against the module/exe location too - otherwise the database silently
        loads empty and every decode shows as an unknown sensor."""
        p = Path(name)
        if p.is_absolute() or p.exists():
            return p
        here = Path(__file__).resolve().parent
        cands = [here / p, here.parent / p]
        exe = Path(getattr(sys, 'executable', '') or '').parent
        if getattr(sys, 'frozen', False) and exe:
            cands[:0] = [exe / p, exe / '_internal' / p]
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            cands.insert(0, Path(meipass) / p)
        for c in cands:
            if c.exists():
                return c
        return p

    def load_sensors(self):
        """Load sensor data from Excel file"""
        if not self.excel_path.exists():
            print(f"[WARNING] {self.excel_path} not found")
            self.load_overrides()   # test-rig/supplementary sensors still load
            return

        try:
            df = pd.read_excel(self.excel_path, engine='openpyxl')

            # Clean and process data
            df['ERRTS ID'] = pd.to_numeric(df['ERRTS ID'], errors='coerce')
            df = df.dropna(subset=['ERRTS ID'])

            # Build lookup tables
            for _, row in df.iterrows():
                errts_id = int(row['ERRTS ID'])

                sensor_info = {
                    'errts_id': errts_id,
                    'site_number': str(row['Site Number']),
                    'site_name': str(row['Site Name']),
                    'sensor_type': str(row['Sensor Type']),
                    'telemetry_type': str(row.get('Telemetry Type', 'Unknown')),
                    'protocol': str(row.get('Protocol', 'ALERT')),
                    'latitude': None,
                    'longitude': None
                }

                self.sensors[errts_id] = sensor_info
                self.errts_to_site[errts_id] = str(row['Site Number'])

            print(f"[OK] Loaded {len(self.sensors)} sensor configurations")

        except Exception as e:
            print(f"[ERROR] Error loading sensors: {e}")

        self.load_overrides()

    def load_overrides(self, filename: str = "sensor_overrides.json"):
        """Load supplementary sensors (test rigs, stations missing from the
        spreadsheet). Entries here win over the Excel data and may carry an
        explicit 'multiplier'/'unit' when a station's configured Scale differs
        from the sensor-type default."""
        p = self._resolve(filename)
        for _ in (0,):
            if not p.exists():
                continue
            try:
                with open(p, 'r', encoding='utf-8') as fh:
                    data = json.load(fh)
                n = 0
                for s in data.get('sensors', []):
                    sid = int(s['errts_id'])
                    info = dict(self.sensors.get(sid, {}))
                    info.update({k: v for k, v in s.items() if k != 'errts_id'})
                    info['errts_id'] = sid
                    info.setdefault('telemetry_type', 'Unknown')
                    info.setdefault('protocol', 'ALERT')
                    info.setdefault('latitude', None)
                    info.setdefault('longitude', None)
                    self.sensors[sid] = info
                    self.errts_to_site[sid] = str(info.get('site_number', ''))
                    n += 1
                if n:
                    print(f"[OK] Loaded {n} sensor overrides from {p.name}")
            except Exception as e:
                print(f"[ERROR] Error loading {p}: {e}")
            return

    def get_sensor_info(self, errts_id: int) -> Optional[Dict]:
        """Get sensor information by ERRTS ID"""
        return self.sensors.get(errts_id)

    def get_sensor_type_info(self, sensor_type: str) -> Dict:
        """Get display information for a sensor type"""
        sensor_types = {
            'Rain': {'unit': 'mm', 'multiplier': 0.254, 'description': 'Rainfall'},
            'Batt': {'unit': 'V', 'multiplier': 0.1, 'description': 'Battery Voltage'},
            'River': {'unit': 'm', 'multiplier': 0.01, 'description': 'Water Level'},
            'River2': {'unit': 'm', 'multiplier': 0.01, 'description': 'Water Level (Alt)'},
            'River4': {'unit': 'm', 'multiplier': 0.01, 'description': 'Water Level (Alt4)'},
            'River5': {'unit': 'm', 'multiplier': 0.01, 'description': 'Water Level (Alt5)'},
            'RiverC': {'unit': 'm', 'multiplier': 0.01, 'description': 'Water Level (C)'},
            'DIS': {'unit': 'm³/s', 'multiplier': 1.0, 'description': 'Discharge'},
            'DO': {'unit': 'mg/L', 'multiplier': 0.1, 'description': 'Dissolved Oxygen'},
            'C': {'unit': '°C', 'multiplier': 0.1, 'description': 'Temperature'},
            'pH': {'unit': 'pH', 'multiplier': 0.1, 'description': 'pH Level'}
        }

        return sensor_types.get(sensor_type, {
            'unit': 'raw',
            'multiplier': 1.0,
            'description': f'Unknown ({sensor_type})'
        })

    def decode_sensor_value(self, errts_id: int, raw_value: int) -> Tuple[float, str, str]:
        """
        Decode raw sensor value to physical units

        Returns: (value, unit, description)
        """
        sensor_info = self.get_sensor_info(errts_id)

        if sensor_info:
            sensor_type = sensor_info.get('sensor_type', '')
            type_info = self.get_sensor_type_info(sensor_type)

            # A per-sensor multiplier/unit (from sensor_overrides.json) wins:
            # a station configured with Scale 1000 reports millimetres while
            # the network default for that type is centimetres.
            mult = sensor_info.get('multiplier', type_info['multiplier'])
            value = raw_value * mult
            unit = sensor_info.get('unit', type_info['unit'])
            description = sensor_info.get('description',
                                          type_info['description'])
        else:
            value = float(raw_value)
            unit = 'raw'
            description = 'Unknown Sensor'

        return value, unit, description

    def format_decoded_value(self, errts_id: int, raw_value: int) -> str:
        """Format decoded value for display"""
        value, unit, description = self.decode_sensor_value(errts_id, raw_value)

        if unit == 'raw':
            return f"{value:.0f} {unit}"
        elif unit == 'm':
            # Show the resolution the station actually transmits: a Scale-1000
            # (millimetre) station needs 3 dp or 0.896 m displays as "0.90 m".
            info = self.get_sensor_info(errts_id) or {}
            dp = 3 if info.get('multiplier', 0.01) <= 0.001 else 2
            return f"{value:.{dp}f} {unit}"
        elif unit in ['V', '°C', 'pH']:
            return f"{value:.2f} {unit}"
        elif unit == 'mm':
            return f"{value:.1f} {unit}"
        else:
            return f"{value:.1f} {unit}"


# Global instance
_db = None

def get_sensor_db(excel_path: str = "Sensors.xlsx") -> SensorDatabase:
    """Get or create global sensor database instance"""
    global _db
    if _db is None:
        _db = SensorDatabase(excel_path)
    return _db


if __name__ == "__main__":
    db = SensorDatabase()
    print("\n[TEST] Sample Sensor Data:")
    print("-" * 80)
    for errts_id in [3000, 3001, 3002, 3005, 3008]:
        info = db.get_sensor_info(errts_id)
        if info:
            print(f"ERRTS {errts_id}: {info['site_name']} - {info['sensor_type']}")
