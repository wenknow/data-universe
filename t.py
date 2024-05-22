import bittensor as bt
# Replace below with your SS58 hotkey
hotkey = "5CtTvMip2GmNBAh1ZTXYGGKDonhaqoe92bVf3FkBNVbapVVp"
network = "finney"
netuid = 13 # subnet uid
sub = bt.subtensor(network)
mg = sub.metagraph(netuid)
if hotkey not in mg.hotkeys:
  print(f"Hotkey {hotkey} deregistered")
else:
  print(f"Hotkey {hotkey} is registered")
