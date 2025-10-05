"""LLM agent for processing user commands and generating responses."""
import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# Initialize OpenAI client
openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def call_agent(input_text: str) -> str:

    response = openai_client.chat.completions.create(
        messages=[
            {
                "role": "system",
                "content": (
                    """You are a personal assistant robot designed to aid the elderly population with everyday tasks. You need to provide 3 things:
(1) a boolean value for whether movement is necessary to follow the user's command
(2) appropriate, helpful verbal output
(3) Motion information - provide detailed step-by-step navigation/guidance instructions when movement is required (e.g., "Move forward 3 steps, turn left at the door, walk 5 steps to the cabinet"). If no movement is needed, put "N/A".

Organize these three outputs as a list separated by %,%
Format: <boolean> %,% <verbal output> %,% <motion plan>
Example: True %,% Let me guide you to the cabinet %,% Move forward 3 steps, turn left at the door, walk 5 steps to the cabinet

Do not put the boolean in quotations. List should be THREE elements long."""
                )
            },
            {
                "role": "user",
                "content": "Provide a response to this input: " + input_text
            }
        ],
        model="gpt-4o",
    )
    return response.choices[0].message.content


def parse_agent_response(raw: str) -> tuple[bool, str, str]:
    parts = [p.strip().strip('"').strip("'") for p in raw.split("%,%")]
    while len(parts) < 3:
        parts.append("N/A")
    parts = parts[:3]
    movement = parts[0].lower() in ("true", "yes", "1")
    return movement, parts[1], parts[2]
