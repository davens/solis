"""Local entry point: the read-only Solis API with the browser dashboard on /.

  uv run --no-project --with flask --with pysolarmanv5 python settings_dash.py

Docker / Home Assistant runs solis_api.py instead - same API, no dashboard.
"""
import dash
import solis_api

if __name__ == "__main__":
    solis_api.app.register_blueprint(dash.dash)
    print(f"Solis dashboard on http://{solis_api.HOST}:{solis_api.PORT}")
    solis_api.serve()
