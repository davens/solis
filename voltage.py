from pysolarmanv5 import PySolarmanV5
import solis_net
import time
import sqlite3
import datetime
import os # To check if DB exists

# --- Configuration ---
IP_ADDRESS = None  # resolved via solis_net.resolve_host()
SERIAL_NUMBER = 0000000000  # Logger's serial number
PORT = 8899
SLAVE_ID = 1
READ_INTERVAL_SECONDS = 5  # Check every 30 seconds
DB_FILE = "solis_voltage_log.db" # Name of the SQLite database file
VOLTAGE_REGISTER = 33073 # Grid Phase A Voltage (Confirm this is correct for your model)
VOLTAGE_SCALING = 0.1    # Scaling factor for voltage register

# --- Database Functions ---

def setup_database(db_file):
    """Creates the SQLite database and table if they don't exist."""
    db_exists = os.path.exists(db_file)
    conn = None
    try:
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        # Create table if it doesn't exist
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS voltage_readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                voltage_phase_a REAL NOT NULL,
                raw_value INTEGER
            )
        ''')
        # Add an index for faster querying by timestamp, if desired
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_timestamp ON voltage_readings (timestamp)
        ''')
        conn.commit()
        if not db_exists:
            print(f"Database '{db_file}' created successfully.")
        else:
            print(f"Database '{db_file}' already exists. Table 'voltage_readings' checked/created.")

    except sqlite3.Error as e:
        print(f"Database error during setup: {e}")
    finally:
        if conn:
            conn.close()

def record_voltage(db_file, timestamp, voltage, raw_value):
    """Inserts a voltage reading into the database."""
    conn = None
    try:
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO voltage_readings (timestamp, voltage_phase_a, raw_value)
            VALUES (?, ?, ?)
        ''', (timestamp, voltage, raw_value))
        conn.commit()
        # print(f"Data recorded: {timestamp} - {voltage:.1f}V") # Optional: print confirmation
    except sqlite3.Error as e:
        print(f"Database error during insert: {e}")
    finally:
        if conn:
            conn.close()

# --- Monitoring Function ---

def monitor_and_store_voltage(ip_address, serial_number, port, slave_id, interval, db_file):
    """
    Continuously monitors Solis inverter voltage and stores it in the database.
    """
    print(f"Starting continuous monitoring of Solis Inverter at {ip_address}...")
    print(f"Read Interval: {interval} seconds")
    print(f"Logging to database: {db_file}")
    print(f"Reading Register: {VOLTAGE_REGISTER} (Grid Phase A Voltage)")
    print("Press Ctrl+C to stop.")

    modbus = None # Initialize modbus instance variable

    while True:
        try:
            # Connect or ensure connection is active
            # PySolarmanV5 might handle reconnections, but instantiating ensures a fresh start if needed
            # If connection drops frequently, consider moving instantiation inside the loop,
            # but this adds overhead. Let's try keeping it outside first.
            if modbus is None:
                 print(f"Attempting to connect to Solis inverter at {ip_address}...")
                 modbus = solis_net.connect(ip_address)
                 # You might add a short delay after initial connection attempt
                 # time.sleep(2)

            # Get current timestamp
            current_time = datetime.datetime.now()
            timestamp_iso = current_time.isoformat()

            # Read the specific register
            # Ensure the function code (0x04 Input or 0x03 Holding) is correct.
            # PySolarmanV5 uses specific methods:
            # read_input_registers -> Function Code 0x04
            # read_holding_registers -> Function Code 0x03
            # Assuming register 33073 is an Input Register (FC04) based on original script
            regs = modbus.read_input_registers(register_addr=VOLTAGE_REGISTER, quantity=1)

            if regs and isinstance(regs, list) and len(regs) > 0:
                raw_value = regs[0]
                # Apply scaling factor
                grid_voltage = raw_value * VOLTAGE_SCALING

                print(f"{current_time.strftime('%Y-%m-%d %H:%M:%S')} - Grid Phase A Voltage: {grid_voltage:.1f}V (Raw={raw_value})")

                # Record the data to SQLite
                record_voltage(db_file, timestamp_iso, grid_voltage, raw_value)

            else:
                # Handle cases where read might fail or return unexpected data
                print(f"{current_time.strftime('%Y-%m-%d %H:%M:%S')} - Failed to read register {VOLTAGE_REGISTER} or received invalid data. Response: {regs}")
                # Consider setting modbus = None here to force reconnection attempt next cycle if persistent errors occur
                # modbus = None

        except KeyboardInterrupt:
            print("\nMonitoring stopped by user.")
            break # Exit the while loop

        except Exception as e:
            print(f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - An error occurred: {e}")
            # Optional: Reset modbus connection on error to attempt reconnection
            print("Attempting to reset connection in the next cycle.")
            modbus = None # Force re-instantiation in the next loop iteration
            # Add a longer pause after an error before retrying
            time.sleep(interval / 2) # Wait a bit before the main sleep

        # Wait for the next interval
        # print(f"Waiting {interval} seconds...") # Can be noisy, uncomment if needed
        time.sleep(interval)

    # Cleanup (optional, PySolarmanV5 might handle socket closing)
    print("Exiting monitoring script.")
    # if modbus:
    #    # Check if a disconnect method exists if needed
    #    pass


# --- Script Execution ---
if __name__ == "__main__":
    # 1. Setup the database (create file and table if necessary)
    setup_database(DB_FILE)

    # 2. Start the monitoring loop
    monitor_and_store_voltage(
        ip_address=IP_ADDRESS or solis_net.resolve_host(),
        serial_number=SERIAL_NUMBER,
        port=PORT,
        slave_id=SLAVE_ID,
        interval=READ_INTERVAL_SECONDS,
        db_file=DB_FILE
    )