DriveGuard website (real data only)

1. Copy the "backend" and "frontend" folders into your DriveGuard folder
   (replace your old backend/main.py).
2. Open Command Prompt in the DriveGuard folder and install once:
      pip install fastapi uvicorn pandas
3. Start:
      python -m uvicorn backend.main:app --reload
4. First start prints 3 logins (admin / engineer / viewer) in the terminal
   and saves them in data\first_run_credentials.txt.
5. Open http://127.0.0.1:8000/app/

Data used: data\processed\driveguard_combined.csv if it exists,
otherwise driveguard_features.csv, otherwise driveguard_cleaned.csv.
Pages that need a trained model say "Not available yet".
