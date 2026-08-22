from flask import Flask, render_template, jsonify
import sqlite3
import os
import pandas as pd
from datetime import datetime, timedelta

app = Flask(__name__)

# Database path - ensure this matches your data collection script
DB_FILE = "solis_voltage_log.db"

def get_db_connection():
    """Create a connection to the SQLite database."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row  # This enables column access by name
    return conn

@app.route('/')
def index():
    """Render the main dashboard page."""
    return render_template('index.html')

@app.route('/data')
def get_data():
    """API endpoint to retrieve voltage data."""
    try:
        conn = get_db_connection()
        
        # Get the last 24 hours of data
        yesterday = (datetime.now() - timedelta(days=1)).isoformat()
        
        df = pd.read_sql_query(
            "SELECT timestamp, voltage_phase_a FROM voltage_readings WHERE timestamp > ? ORDER BY timestamp",
            conn, 
            params=(yesterday,)
        )
        
        conn.close()
        
        # Convert timestamps to a format Chart.js can easily use
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df['formatted_time'] = df['timestamp'].dt.strftime('%H:%M:%S')
        
        # Calculate statistics
        if not df.empty:
            stats = {
                'current': df['voltage_phase_a'].iloc[-1],
                'min': df['voltage_phase_a'].min(),
                'max': df['voltage_phase_a'].max(),
                'avg': df['voltage_phase_a'].mean()
            }
        else:
            stats = {'current': 0, 'min': 0, 'max': 0, 'avg': 0}
        
        # Format data for Chart.js
        chart_data = {
            'labels': df['formatted_time'].tolist(),
            'datasets': [{
                'label': 'Grid Voltage (V)',
                'data': df['voltage_phase_a'].tolist(),
                'borderColor': 'rgba(75, 192, 192, 1)',
                'backgroundColor': 'rgba(75, 192, 192, 0.2)',
                'tension': 0.1
            }]
        }
        
        return jsonify({
            'chartData': chart_data,
            'stats': stats
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/stats')
def get_stats():
    """API endpoint to retrieve summary statistics."""
    try:
        conn = get_db_connection()
        
        # Calculate statistics from all available data
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                COUNT(*) as count,
                MIN(voltage_phase_a) as min,
                MAX(voltage_phase_a) as max,
                AVG(voltage_phase_a) as avg,
                (SELECT voltage_phase_a FROM voltage_readings ORDER BY timestamp DESC LIMIT 1) as latest
            FROM voltage_readings
        """)
        
        stats = cursor.fetchone()
        conn.close()
        
        return jsonify({
            'count': stats['count'],
            'min': round(stats['min'], 1),
            'max': round(stats['max'], 1),
            'avg': round(stats['avg'], 1),
            'latest': round(stats['latest'], 1) if stats['latest'] else 0,
            'updated': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# Create templates directory and HTML files
os.makedirs('templates', exist_ok=True)

# Create index.html
with open('templates/index.html', 'w') as f:
    f.write("""<!DOCTYPE html>
<html>
<head>
    <title>Solis Inverter Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0-alpha1/dist/css/bootstrap.min.css" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body {
            padding-top: 20px;
            background-color: #f5f5f5;
        }
        .card {
            margin-bottom: 20px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }
        .stats-card {
            text-align: center;
            transition: all 0.3s ease;
        }
        .stats-card:hover {
            transform: translateY(-5px);
        }
        .voltage-current {
            font-size: 2.5rem;
            font-weight: bold;
            color: #2c3e50;
        }
        .voltage-unit {
            font-size: 1rem;
            color: #7f8c8d;
        }
        .stats-label {
            color: #7f8c8d;
            font-size: 0.9rem;
            text-transform: uppercase;
        }
        .stats-value {
            font-size: 1.5rem;
            font-weight: bold;
            color: #2c3e50;
        }
        .navbar-brand {
            font-weight: bold;
        }
        #lastUpdated {
            font-size: 0.8rem;
            color: #7f8c8d;
        }
    </style>
</head>
<body>
    <div class="container">
        <nav class="navbar navbar-expand-lg navbar-light bg-light rounded mb-4">
            <div class="container-fluid">
                <a class="navbar-brand" href="#">Solis Inverter Dashboard</a>
                <div class="d-flex">
                    <span id="lastUpdated">Last updated: --</span>
                </div>
            </div>
        </nav>
        
        <div class="row mb-4">
            <div class="col-md-3">
                <div class="card stats-card">
                    <div class="card-body">
                        <h5 class="stats-label">Current Voltage</h5>
                        <div class="voltage-current" id="currentVoltage">--<span class="voltage-unit">V</span></div>
                    </div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card stats-card">
                    <div class="card-body">
                        <h5 class="stats-label">Minimum</h5>
                        <div class="stats-value" id="minVoltage">--<span class="voltage-unit">V</span></div>
                    </div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card stats-card">
                    <div class="card-body">
                        <h5 class="stats-label">Maximum</h5>
                        <div class="stats-value" id="maxVoltage">--<span class="voltage-unit">V</span></div>
                    </div>
                </div>
            </div>
            <div class="col-md-3">
                <div class="card stats-card">
                    <div class="card-body">
                        <h5 class="stats-label">Average</h5>
                        <div class="stats-value" id="avgVoltage">--<span class="voltage-unit">V</span></div>
                    </div>
                </div>
            </div>
        </div>
        
        <div class="row">
            <div class="col-md-12">
                <div class="card">
                    <div class="card-header">
                        Grid Voltage (24 Hours)
                    </div>
                    <div class="card-body">
                        <canvas id="voltageChart" height="300"></canvas>
                    </div>
                </div>
            </div>
        </div>
    </div>
    
    <script>
        let voltageChart;
        
        // Initialize chart
        function initChart() {
            const ctx = document.getElementById('voltageChart').getContext('2d');
            voltageChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: [],
                    datasets: [{
                        label: 'Grid Voltage (V)',
                        data: [],
                        borderColor: 'rgba(75, 192, 192, 1)',
                        backgroundColor: 'rgba(75, 192, 192, 0.2)',
                        tension: 0.1,
                        borderWidth: 2
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {
                        y: {
                            beginAtZero: false
                        }
                    },
                    plugins: {
                        tooltip: {
                            mode: 'index',
                            intersect: false
                        }
                    }
                }
            });
        }
        
        // Fetch data and update chart
        function updateData() {
            fetch('/data')
                .then(response => response.json())
                .then(data => {
                    if (data.error) {
                        console.error('Error fetching data:', data.error);
                        return;
                    }
                    
                    // Update chart
                    voltageChart.data.labels = data.chartData.labels;
                    voltageChart.data.datasets[0].data = data.chartData.datasets[0].data;
                    voltageChart.update();
                    
                    // Update stats
                    document.getElementById('currentVoltage').innerHTML = 
                        data.stats.current.toFixed(1) + '<span class="voltage-unit">V</span>';
                    document.getElementById('minVoltage').innerHTML = 
                        data.stats.min.toFixed(1) + '<span class="voltage-unit">V</span>';
                    document.getElementById('maxVoltage').innerHTML = 
                        data.stats.max.toFixed(1) + '<span class="voltage-unit">V</span>';
                    document.getElementById('avgVoltage').innerHTML = 
                        data.stats.avg.toFixed(1) + '<span class="voltage-unit">V</span>';
                    
                    // Update timestamp
                    document.getElementById('lastUpdated').innerText = 
                        'Last updated: ' + new Date().toLocaleTimeString();
                })
                .catch(error => {
                    console.error('Error:', error);
                });
        }
        
        // Initialize page
        document.addEventListener('DOMContentLoaded', function() {
            initChart();
            updateData();
            
            // Refresh data every 30 seconds
            setInterval(updateData, 5000);
        });
    </script>
</body>
</html>""")

if __name__ == '__main__':
    print("Solis Inverter Dashboard starting...")
    print(f"Looking for database at: {os.path.abspath(DB_FILE)}")
    
    if not os.path.exists(DB_FILE):
        print(f"Warning: Database file '{DB_FILE}' not found.")
        print("The dashboard will start, but no data will be displayed until the database is created.")
        print("Make sure your data collection script is running and creating the database.")
    else:
        print(f"Database found. Dashboard is ready.")
    
    print("\nOpen your browser and navigate to http://127.0.0.1:5050")
    print("Press CTRL+C to stop the dashboard")
    
    # Start the Flask development server
    app.run(debug=True, host='0.0.0.0', port=5050)