import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import os

def generate_day(date_str, open_price, trend='up'):
    date = pd.to_datetime(date_str)
    times = [date + timedelta(hours=9, minutes=15+i) for i in range(375)]
    
    df = pd.DataFrame({'timestamp': times})
    df['instrument'] = 'NIFTY'
    df['volume'] = 1000
    
    current_price = open_price
    opens, highs, lows, closes = [], [], [], []
    
    for i in range(375):
        # 09:15 to 09:30 choppy
        if i < 15:
            change = np.random.uniform(-5, 5)
        # 09:30 to 10:00 trend breakout
        elif i < 45:
            change = np.random.uniform(2, 10) if trend == 'up' else np.random.uniform(-10, -2)
        # rest of day flat
        else:
            change = np.random.uniform(-2, 2)
            
        o = current_price
        c = current_price + change
        h = max(o, c) + abs(np.random.normal(0, 1))
        l = min(o, c) - abs(np.random.normal(0, 1))
        
        opens.append(o)
        highs.append(h)
        lows.append(l)
        closes.append(c)
        current_price = c
        
    df['open'] = opens
    df['high'] = highs
    df['low'] = lows
    df['close'] = closes
    return df

np.random.seed(42)
days = [
    generate_day('2024-01-01', 21000, 'up'),
    generate_day('2024-01-02', 21200, 'down'),
    generate_day('2024-01-03', 21100, 'up')
]

df = pd.concat(days)
os.makedirs('data/raw', exist_ok=True)
df.to_csv('data/raw/NIFTY.csv', index=False)
print("Synthetic data saved to data/raw/NIFTY.csv")
