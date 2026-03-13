#!/usr/bin/env python3
"""
GPS Logger via gpsd - 10Hz位置情報取得・記録スクリプト
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
import time
import subprocess

class GPSLogger:
    def __init__(self, output_dir='/usb/data/gpslog/', gpsd_host='127.0.0.1', gpsd_port=2947, device_path='/dev/ttyGPS'):
        self.output_dir = Path(output_dir)
        self.gpsd_host = gpsd_host
        self.gpsd_port = gpsd_port
        self.device_path = device_path
        self.gpsd_module = None
        self.raw_log_file = None
        self.json_log_file = None
        
    def setup_output_directory(self):
        """出力ディレクトリの作成"""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        print(f"Output directory: {self.output_dir}")
    
    def configure_device_only(self, baudrate=9600):
        """GPSデバイスを10Hzに設定（gpsdを停止して実行）"""
        import serial
        
        print("=" * 60)
        print("GPS Device Configuration Mode")
        print("=" * 60)
        
        # gpsdの状態確認
        print("\nChecking gpsd status...")
        gpsd_was_active = False
        try:
            result = subprocess.run(['systemctl', 'is-active', 'gpsd'], 
                                  capture_output=True, text=True)
            gpsd_was_active = result.stdout.strip() == 'active'
            
            if gpsd_was_active:
                print("gpsd is running. Stopping gpsd...")
                subprocess.run(['sudo', 'systemctl', 'stop', 'gpsd'], check=True)
                time.sleep(2)
                print("gpsd stopped.")
            else:
                print("gpsd is not running.")
        except subprocess.CalledProcessError as e:
            print(f"Warning: Could not check/stop gpsd: {e}")
            print("Continuing anyway...")
        
        # シリアルポートを開く
        print(f"\nOpening device: {self.device_path}")
        try:
            # pyubx2のインポートをここで行う（設定時のみ必要）
            try:
                from pyubx2 import UBXMessage, SET
            except ImportError:
                print("Error: pyubx2 is not installed.")
                print("Install it with: pip3 install pyubx2 --break-system-packages")
                return False
            
            serial_port = serial.Serial(
                self.device_path,
                baudrate=baudrate,
                timeout=1
            )
            print(f"Device opened successfully")
            time.sleep(0.5)
            
            # UBXデバイスを10Hz動作に設定
            print("\nConfiguring GPS for 10Hz operation...")
            
            # CFG-RATE: 測定レート設定 (100ms = 10Hz)
            print("  - Setting measurement rate to 100ms (10Hz)...")
            cfg_rate = UBXMessage('CFG', 'CFG-RATE', SET, 
                                  measRate=100, navRate=1, timeRef=1)
            serial_port.write(cfg_rate.serialize())
            time.sleep(0.3)
            
            # CFG-MSG: UBX-NAV-PVT メッセージを有効化（全ポートで出力）
            print("  - Enabling UBX-NAV-PVT messages...")
            cfg_msg = UBXMessage('CFG', 'CFG-MSG', SET,
                                msgClass=0x01, msgID=0x07, rateUART1=1)
            serial_port.write(cfg_msg.serialize())
            time.sleep(0.3)
            
            # NMEA GGAメッセージも有効化（バックアップ用）
            print("  - Enabling NMEA GGA messages...")
            cfg_msg_gga = UBXMessage('CFG', 'CFG-MSG', SET,
                                    msgClass=0xF0, msgID=0x00, rateUART1=1)
            serial_port.write(cfg_msg_gga.serialize())
            time.sleep(0.3)
            
            # NMEA RMCメッセージも有効化
            print("  - Enabling NMEA RMC messages...")
            cfg_msg_rmc = UBXMessage('CFG', 'CFG-MSG', SET,
                                    msgClass=0xF0, msgID=0x04, rateUART1=1)
            serial_port.write(cfg_msg_rmc.serialize())
            time.sleep(0.3)


            # CFG-CFG: 設定を永続化
            # 正しい属性名を使用
            print("  - Saving configuration to device flash memory...")
            try:
                # まず、正しいパラメータ名を試す
                cfg_cfg = UBXMessage('CFG', 'CFG-CFG', SET,
                                    saveMask=0x0000061F,  # ioPort|msgConf|infMsg|navConf|rxmConf|rinvConf
                                    loadMask=0x00000000,
                                    deviceMask=0x00000017)  # devBBR|devFlash|devEEPROM|devSpiFlash
                serial_port.write(cfg_cfg.serialize())
                time.sleep(1.0)
            except Exception as e1:
                print(f"    First attempt failed: {e1}")
                try:
                    # 代替方法：バイナリで直接送信
                    print("    Trying alternative method...")
                    # UBX-CFG-CFG メッセージを手動構築
                    # Header: 0xB5 0x62, Class: 0x06, ID: 0x09, Length: 0x0D 0x00
                    # Payload: clearMask(4), saveMask(4), loadMask(4), deviceMask(1)
                    header = bytes([0xB5, 0x62, 0x06, 0x09, 0x0D, 0x00])
                    clearMask = bytes([0x00, 0x00, 0x00, 0x00])  # Don't clear
                    saveMask = bytes([0x1F, 0x06, 0x00, 0x00])   # Save all
                    loadMask = bytes([0x00, 0x00, 0x00, 0x00])   # Don't load
                    deviceMask = bytes([0x17])                    # All devices
                    
                    payload = clearMask + saveMask + loadMask + deviceMask
                    
                    # チェックサム計算
                    ck_a = 0
                    ck_b = 0
                    for b in header[2:] + payload:
                        ck_a = (ck_a + b) & 0xFF
                        ck_b = (ck_b + ck_a) & 0xFF
                    
                    message = header + payload + bytes([ck_a, ck_b])
                    serial_port.write(message)
                    time.sleep(1.0)
                    print("    Configuration saved using alternative method")
                except Exception as e2:
                    print(f"    Warning: Could not save to flash: {e2}")
                    print("    Configuration is active but may not persist after reboot")
        
           
            serial_port.close()
            print("\n✓ Configuration completed and saved to device")
            
        except serial.SerialException as e:
            print(f"\n✗ Error opening serial port: {e}")
            print(f"  Make sure {self.device_path} exists and you have permissions.")
            return False
        except Exception as e:
            print(f"\n✗ Error during configuration: {e}")
            return False
        finally:
            # gpsdを再起動（停止していた場合）
            try:
                if gpsd_was_active:
                    print("\nRestarting gpsd...")
                    subprocess.run(['sudo', 'systemctl', 'start', 'gpsd'], check=True)
                    time.sleep(2)
                    print("gpsd restarted.")
            except subprocess.CalledProcessError as e:
                print(f"Warning: Could not restart gpsd: {e}")
                print("You may need to restart it manually: sudo systemctl start gpsd")
        
        print("\n" + "=" * 60)
        print("Configuration complete!")
        print("=" * 60)
        print("\nThe GPS device is now configured for 10Hz operation.")
        print("Settings have been saved to device flash memory.")
        print("\nYou can now run the logger:")
        print(f"  python3 {sys.argv[0]} --output {self.output_dir}")
        print("\n")
        
        return True
        
    def connect_gpsd(self):
        """gpsdに接続"""
        try:
            import gpsd
            self.gpsd_module = gpsd
            
            # gpsd-py3の接続方法
            gpsd.connect(host=self.gpsd_host, port=self.gpsd_port)
            print(f"Connected to gpsd at {self.gpsd_host}:{self.gpsd_port}")
            return True
            
        except ImportError:
            print("Error: gpsd module is not installed.")
            print("\nInstall with:")
            print("  pip3 install gpsd-py3 --break-system-packages")
            return False
        except Exception as e:
            print(f"Error connecting to gpsd: {e}")
            print("\nTroubleshooting:")
            print("  1. Check if gpsd is running:")
            print("     sudo systemctl status gpsd")
            print("  2. Check if GPS device is connected:")
            print(f"     ls -l {self.device_path}")
            print("  3. Test gpsd manually:")
            print("     cgps -s")
            return False
    
    def create_log_files(self):
        """ログファイルを作成"""
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S.%f')[:-3]
        
        raw_log_path = self.output_dir / f"{timestamp}_raw.log"
        json_log_path = self.output_dir / f"{timestamp}_pos.log"
        
        self.raw_log_file = open(raw_log_path, 'w', buffering=1)
        self.json_log_file = open(json_log_path, 'w', buffering=1)
        
        print(f"Raw log: {raw_log_path}")
        print(f"JSON log: {json_log_path}")
    
    def run(self):
        """メイン処理ループ"""
        self.setup_output_directory()
        
        if not self.connect_gpsd():
            return 1
        
        try:
            self.create_log_files()
            
            print("\nLogging started. Press Ctrl+C to stop.")
            print("Note: Update rate depends on gpsd configuration\n")
            
            while True:
                try:
                    # gpsd-py3のget_current()を使用
                    packet = self.gpsd_module.get_current()
                    
                    if packet:
                        timestamp = datetime.now().isoformat(timespec='milliseconds')
                        
                        # 生データをログ保存
                        raw_str = str(packet.__dict__ if hasattr(packet, '__dict__') else packet)
                        self.raw_log_file.write(f"{timestamp} GPSD: {raw_str}\n")
                        
                        # TPVデータ（位置・速度・時刻）の処理
                        if hasattr(packet, 'mode') and packet.mode >= 2:  # 2D fix以上
                            json_data = {
                                'type': 'GPSD_TPV',
                                'system_timestamp': timestamp,
                                'mode': packet.mode
                            }
                            
                            # 時刻情報
                            if hasattr(packet, 'time') and packet.time:
                                json_data['time'] = packet.time
                            
                            # 位置情報
                            if hasattr(packet, 'lat') and packet.lat != 0:
                                json_data['latitude'] = packet.lat
                            if hasattr(packet, 'lon') and packet.lon != 0:
                                json_data['longitude'] = packet.lon
                            if hasattr(packet, 'alt') and packet.alt is not None:
                                json_data['altitude'] = packet.alt
                            
                            # 移動情報
                            if hasattr(packet, 'speed') and packet.speed is not None:
                                json_data['speed'] = packet.speed()
                            if hasattr(packet, 'track') and packet.track is not None:
                                json_data['track'] = packet.track
                            if hasattr(packet, 'climb') and packet.climb is not None:
                                json_data['climb'] = packet.climb
                            
                            # 精度情報
                            if hasattr(packet, 'epx') and packet.epx is not None:
                                json_data['error_x'] = packet.epx
                            if hasattr(packet, 'epy') and packet.epy is not None:
                                json_data['error_y'] = packet.epy
                            if hasattr(packet, 'epv') and packet.epv is not None:
                                json_data['error_altitude'] = packet.epv
                            if hasattr(packet, 'eps') and packet.eps is not None:
                                json_data['error_speed'] = packet.eps
                            if hasattr(packet, 'ept') and packet.ept is not None:
                                json_data['error_time'] = packet.ept
                            
                            # 衛星情報
                            if hasattr(packet, 'sats') and packet.sats is not None:
                                json_data['num_satellites'] = packet.sats
                            if hasattr(packet, 'sats_valid') and packet.sats_valid is not None:
                                json_data['num_satellites_used'] = packet.sats_valid
                            
                            if 'latitude' in json_data:
                                self.json_log_file.write(json.dumps(json_data, ensure_ascii=False) + '\n')
                                
                                lat = json_data.get('latitude', 0)
                                lon = json_data.get('longitude', 0)
                                speed = json_data.get('speed', 0)
                                track = json_data.get('track', 0)
                                mode = json_data.get('mode', 0)
                                sats = json_data.get('num_satellites', 0)
                                
                                print(f"TPV: Lat={lat:.6f}, Lon={lon:.6f}, "
                                      f"Speed={speed:.2f}m/s, Track={track:.2f}  Mode={mode}, Sats={sats}    ", end='\r')
                    
                    time.sleep(0.05)  # 20Hzでポーリング（GPSが10Hzなので十分）
                    
                except Exception as e:
                    print(f"\nError reading data: {e}")
                    time.sleep(0.1)
                    continue
                    
        except KeyboardInterrupt:
            print("\n\nStopping logger...")
        finally:
            if self.raw_log_file:
                self.raw_log_file.close()
            if self.json_log_file:
                self.json_log_file.close()
            print("Logger stopped.")
        
        return 0

def main():
    parser = argparse.ArgumentParser(
        description='GPS Logger via gpsd - 位置情報取得・記録',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # デバイスを10Hzに設定（最初に1回実行）
  sudo python3 %(prog)s --configure-only
  
  # ログ記録を開始
  python3 %(prog)s --output /usb/data/gpslog/
  
  # カスタムデバイスパスで設定
  sudo python3 %(prog)s --configure-only --device /dev/ttyUSB0 --baudrate 115200
        """
    )
    
    parser.add_argument(
        '--configure-only',
        action='store_true',
        help='Configure GPS device for 10Hz operation and exit (stops gpsd temporarily)'
    )
    
    parser.add_argument(
        '--device',
        default='/dev/ttyGPS',
        help='GPS device path (default: /dev/ttyGPS, used only with --configure-only)'
    )
    
    parser.add_argument(
        '--baudrate',
        type=int,
        default=9600,
        help='Serial baudrate (default: 9600, used only with --configure-only)'
    )
    
    parser.add_argument(
        '--output',
        default='/usb/data/gpslog/',
        help='Output directory (default: /usb/data/gpslog/)'
    )
    
    parser.add_argument(
        '--host',
        default='127.0.0.1',
        help='gpsd host (default: 127.0.0.1)'
    )
    
    parser.add_argument(
        '--port',
        type=int,
        default=2947,
        help='gpsd port (default: 2947)'
    )
    
    args = parser.parse_args()
    
    logger = GPSLogger(
        output_dir=args.output,
        gpsd_host=args.host,
        gpsd_port=args.port,
        device_path=args.device
    )
    
    # 設定モード
    if args.configure_only:
        success = logger.configure_device_only(baudrate=args.baudrate)
        sys.exit(0 if success else 1)
    
    # 通常のログ記録モード
    sys.exit(logger.run())

if __name__ == '__main__':
    main()

