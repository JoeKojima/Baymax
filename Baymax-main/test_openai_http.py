import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")
print("API key present?", bool(api_key))

client = OpenAI(api_key=api_key)
models = client.models.list()
print("Got", len(models.data), "models")