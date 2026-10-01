import pandas as pd
import matplotlib.pyplot as plt

# Load data
df = pd.read_csv("shared/v2i_log.csv")

# Calculate packet loss via sequence numbers
min_seq = df['seq'].min()
max_seq = df['seq'].max()
expected_seqs = set(range(min_seq, max_seq + 1))
received_seqs = set(df['seq'])
lost_seqs = expected_seqs - received_seqs
loss_percentage = (len(lost_seqs) / len(expected_seqs)) * 100

print(f"Total Expected Packets: {len(expected_seqs)}")
print(f"Total Lost Packets: {len(lost_seqs)} ({loss_percentage:.2f}%)")

# Time normalization
# T=0 is calculated by finding the largest time gap in received packets (the blackout window)
df['rx_ts_s'] = df['rx_ts_ns'] / 1e9
df = df.sort_values('rx_ts_s').reset_index(drop=True)
df['time_diff'] = df['rx_ts_s'].diff()

failover_idx = df['time_diff'].idxmax()
# Estimate failover trigger just before the blackout window
failover_time = df.loc[failover_idx, 'rx_ts_s'] - df.loc[failover_idx, 'time_diff']

df['relative_time'] = df['rx_ts_s'] - failover_time

# Generate Plot
plt.figure(figsize=(10, 6))
plt.scatter(df['relative_time'], df['delay_ms'], marker='.', alpha=0.7)
plt.axvline(x=0, color='r', linestyle='--', label='Failover Trigger (T=0)')
plt.title('V2I Telemetry Latency During Failover')
plt.xlabel('Time Relative to Failover (seconds)')
plt.ylabel('Delay (ms)')
plt.grid(True)
plt.legend()
plt.savefig('failover_latency.png')
print("Graph saved as failover_latency.png")