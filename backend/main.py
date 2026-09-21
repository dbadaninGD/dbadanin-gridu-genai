import os
import json
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from google import genai
from google.genai import types
from sqlalchemy import create_engine, text
from langfuse.decorators import observe

# Environment Setup
PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT", "default-project")
LOCATION = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
DB_URL = os.getenv("DATABASE_URL", "postgresql://postgres:password@db:5432/datagen_db")

# Initialize Clients
app = FastAPI(title="Synthetic Data Engine")
engine = create_engine(DB_URL)

# Initialize Google GenAI SDK for Vertex AI
try:
    ai_client = genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)
except Exception as e:
    print(f"Warning: GenAI Client initialization failed. Check credentials. {e}")
    ai_client = None

class GenerateRequest(BaseModel):
    prompt: str
    ddl_schema: str
    temperature: float = 0.7
    max_tokens: int = 1000

class RefineRequest(BaseModel):
    table_name: str
    instructions: str
    current_data: list

@observe(name="generate_synthetic_data")
def call_gemini_model(prompt: str, ddl: str, temp: float, max_tok: int) -> dict:
    """Calls Gemini via Vertex AI to generate JSON data."""
    if not ai_client:
        raise HTTPException(status_code=500, detail="GenAI client not initialized.")
        
    system_instruction = """
    You are a strictly compliant SQL data generator. 
    Analyze the provided DDL. Generate a JSON object where keys are table names and 
    values are arrays of JSON objects representing rows. Respect all primary and foreign keys, 
    data types, and constraints.
    """
    
    config = types.GenerateContentConfig(
        system_instruction=[system_instruction],
        temperature=temp,
        max_output_tokens=max_tok,
        response_mime_type="application/json"
    )
    
    try:
        response = ai_client.models.generate_content(
            model='gemini-2.5-pro',
            contents=[f"DDL:\n{ddl}\n\nInstructions:\n{prompt}"],
            config=config
        )
        return json.loads(response.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/generate")
def generate_data(req: GenerateRequest):
    """Endpoint to generate and store synthetic data."""
    generated_json = call_gemini_model(req.prompt, req.ddl_schema, req.temperature, req.max_tokens)
    
    # Store data in PostgreSQL (simplified insert for demonstration)
    with engine.begin() as conn:
        for table, rows in generated_json.items():
            if not rows: continue
            columns = ", ".join(rows[0].keys())
            for row in rows:
                vals = ", ".join([f"'{str(v)}'" for v in row.values()])
                # Note: In production, use parameterized queries to prevent SQL injection
                conn.execute(text(f"INSERT INTO {table} ({columns}) VALUES ({vals}) ON CONFLICT DO NOTHING;"))
                
    return {"status": "success", "data": generated_json}

@app.post("/api/refine")
@observe(name="refine_synthetic_data")
def refine_data(req: RefineRequest):
    """Endpoint to refine generated data based on quick edit instructions."""
    config = types.GenerateContentConfig(response_mime_type="application/json")
    prompt = f"Modify this JSON array based on these instructions: '{req.instructions}'.\nData:\n{json.dumps(req.current_data)}"
    
    response = ai_client.models.generate_content(
        model='gemini-2.5-flash',
        contents=[prompt],
        config=config
    )
    return {"status": "success", "data": json.loads(response.text)}


class QueryRequest(BaseModel):
    question: str

def get_database_schema(connection) -> str:
    """Extracts table names and column details from the active database."""
    schema_query = """
    SELECT table_name, column_name, data_type 
    FROM information_schema.columns 
    WHERE table_schema = 'public';
    """
    result = connection.execute(text(schema_query)).fetchall()
    
    schema_dict = {}
    for table, col, dtype in result:
        if table not in schema_dict:
            schema_dict[table] = []
        schema_dict[table].append(f"{col} ({dtype})")
    
    schema_str = ""
    for table, cols in schema_dict.items():
        schema_str += f"Table: {table}\nColumns: {', '.join(cols)}\n\n"
    return schema_str

@app.post("/api/query")
@observe(name="nl_to_sql_query")
def query_database(req: QueryRequest):
    """Translates natural language to SQL, executes it, and returns the data."""
    if not ai_client:
        raise HTTPException(status_code=500, detail="GenAI client not initialized.")
        
    try:
        with engine.connect() as conn:
            # 1. Retrieve current schema context
            schema_context = get_database_schema(conn)
            
            # 2. Generate SQL using Gemini
            system_instruction = f"""
            You are a PostgreSQL expert. Given the following database schema, translate the user's natural language question into a valid, read-only SQL SELECT query. 
            Return ONLY the raw SQL string. Do not include markdown formatting, backticks, or explanations.
            
            Schema:
            {schema_context}
            """
            
            config = types.GenerateContentConfig(
                system_instruction=[system_instruction],
                temperature=0.1, # Low temperature for factual precision
                max_output_tokens=500
            )
            
            response = ai_client.models.generate_content(
                model='gemini-2.5-pro',
                contents=[req.question],
                config=config
            )
            
            sql_query = response.text.strip()
            # Strip potential markdown formatting if the model disobeys instructions
            if sql_query.startswith("```sql"):
                sql_query = sql_query.replace("```sql", "").replace("```", "").strip()
            
            # 3. Execute the generated SQL
            result = conn.execute(text(sql_query))
            columns = result.keys()
            rows = [dict(zip(columns, row)) for row in result.fetchall()]
            
            return {
                "status": "success", 
                "query_executed": sql_query, 
                "data": rows
            }
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Query execution failed: {str(e)}")
