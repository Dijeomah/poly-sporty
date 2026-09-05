from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

# Your private key from .env
#private_key = "18f9e49ee8dbc3965bb55275b6ceda9184848fa0f2a9d5ddc273af46b2963b68"
private_key = "b1bbb064fb59f07e3cb15ba206b0a9d4d93ce5ae72bf1427ee41803addb5eccb"

# Initialize client
client = ClobClient(
    host="https://clob.polymarket.com",
    key=private_key,
    chain_id=137
)

# Create API credentials
creds = client.create_api_key()

print("Save these credentials:")
print(f"API Key: {creds.api_key}")
print(f"API Secret: {creds.api_secret}")  
print(f"API Passphrase: {creds.api_passphrase}")
