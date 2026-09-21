# Data Assistant - Frontend

## Architecture
A stateless Streamlit application that provides the user interface for schema upload, configuration, data preview, and iterative refinements. It maintains the current state of generated datasets in the user's active session (`st.session_state`) before optionally committing final changes.

## How to use it
1. Start via Docker Compose: `docker-compose up frontend`.
2. Navigate to `http://localhost:8501`.
3. Upload a schema file, tweak parameters, and hit Generate.
4. Use the preview table to view data, and the text box at the bottom to issue edit commands like "Change all values in the 'Status' column to Active".

## Limitations
- **Session Volatility:** Reloading the browser clears the Streamlit session state, losing any generated but un-downloaded data previews. 
- **Large Render Times:** Loading pandas dataframes exceeding several thousand rows directly into the Streamlit UI can cause browser sluggishness.
