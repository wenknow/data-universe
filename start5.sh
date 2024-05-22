#!/bin/bash 
. venv/bin/activate
python3 ./neurons/miner.py --subtensor.network local --wallet.name my_coldkey --wallet.hotkey my_first_hotkey --neuron.scraping_config_file ./scraping/config/miner_11301.json --axon.port 9705 --axon.max_workers 128 --logging.trace --logging.debug
# python3 ./neurons/miner.py --subtensor.network local --wallet.name my_coldkey --wallet.hotkey my_first_hotkey --axon.port 11301 --axon.ip 127.0.0.1 --axon.external_port 9701 --axon.external_ip 85.239.232.142 --axon.max_workers 128 --subtensor.chain_endpoint 127.0.0.1:9944  --logging.trace --logging.debug
