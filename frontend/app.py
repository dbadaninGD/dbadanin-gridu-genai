import streamlit as st
import requests
import pandas as pd
import json
from io import BytesIO
import zipfile

# Configuration
API_URL = "http://backend:8000/api"
st.set_page_config(page_title="Data Assistant", layout="wide")

# Sidebar navigation
st.sidebar.title("Data Assistant")
nav_selection = st.sidebar.radio("Navigation", ["Data Generation", "Talk to your data"], label_visibility="collapsed")

if nav_selection == "Data Generation":
    st.subheader("Prompt")
    user_prompt = st.text_input("Enter your prompt here...", label_visibility="collapsed")
    
    uploaded_file = st.file_uploader("Upload DDL Schema", type=["sql", "txt", "ddl", "json"])
    
    st.subheader("Advanced Parameters")
    col1, col2 = st.columns([3, 1])
    with col1:
        temperature = st.slider("Temperature", min_value=0.0, max_value=1.0, value=0.7, step=0.1)
    with col2:
        max_tokens = st.number_input("Max Tokens", value=1000, step=100)
        
    if st.button("Generate", type="primary"):
        if uploaded_file and user_prompt:
            ddl_content = uploaded_file.getvalue().decode("utf-8")
            payload = {
                "prompt": user_prompt,
                "ddl_schema": ddl_content,
                "temperature": temperature,
                "max_tokens": max_tokens
            }
            with st.spinner("Generating synthetic data..."):
                try:
                    res = requests.post(f"{API_URL}/generate", json=payload)
                    res.raise_for_status()
                    st.session_state['generated_data'] = res.json().get('data', {})
                    st.success("Data generated successfully!")
                except requests.exceptions.RequestException as e:
                    st.error(f"Error generating data: {e}")
        else:
            st.warning("Please provide both a prompt and a DDL schema.")

    # Data Preview Section
    if 'generated_data' in st.session_state and st.session_state['generated_data']:
        st.divider()
        st.subheader("Data Preview")
        
        tables = list(st.session_state['generated_data'].keys())
        selected_table = st.selectbox("Select Table", tables, label_visibility="collapsed")
        
        current_table_data = st.session_state['generated_data'][selected_table]
        df = pd.DataFrame(current_table_data)
        st.dataframe(df, use_container_width=True)
        
        # Download features
        csv_buffer = df.to_csv(index=False).encode('utf-8')
        st.download_button(label="Download CSV", data=csv_buffer, file_name=f"{selected_table}.csv", mime="text/csv")
        
        # Refinement UI matching Screenshot 2026-09-17 at 15.32.28_2.png
        edit_col1, edit_col2 = st.columns([5, 1])
        with edit_col1:
            edit_prompt = st.text_input("Enter quick edit instructions...", key="edit_input", label_visibility="collapsed")
        with edit_col2:
            if st.button("Submit", use_container_width=True):
                with st.spinner("Applying changes..."):
                    refine_payload = {
                        "table_name": selected_table,
                        "instructions": edit_prompt,
                        "current_data": current_table_data
                    }
                    try:
                        refine_res = requests.post(f"{API_URL}/refine", json=refine_payload)
                        refine_res.raise_for_status()
                        st.session_state['generated_data'][selected_table] = refine_res.json().get('data', [])
                        st.rerun()
                    except requests.exceptions.RequestException as e:
                        st.error(f"Error applying changes: {e}")

elif nav_selection == "Talk to your data":
    st.subheader("Talk to your data")
    st.write("Ask questions about your generated synthetic data using plain English.")
    
    # Initialize chat history for this session
    if "messages" not in st.session_state:
        st.session_state.messages = []

    # Display chat messages from history
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if "dataframe" in message:
                st.dataframe(message["dataframe"], use_container_width=True)
            if "sql" in message:
                with st.expander("View SQL Query"):
                    st.code(message["sql"], language="sql")

    # Accept user input
    if prompt := st.chat_input("E.g., Show me the top 5 users by total value..."):
        # Add user message to chat history
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        # Call backend for response
        with st.chat_message("assistant"):
            with st.spinner("Analyzing data..."):
                try:
                    res = requests.post(f"{API_URL}/query", json={"question": prompt})
                    res.raise_for_status()
                    
                    response_data = res.json()
                    sql_executed = response_data.get("query_executed", "")
                    df_result = pd.DataFrame(response_data.get("data", []))
                    
                    if df_result.empty:
                        reply_text = "The query executed successfully, but returned no results."
                        st.markdown(reply_text)
                        st.session_state.messages.append({"role": "assistant", "content": reply_text})
                    else:
                        reply_text = f"Here are the results for your query:"
                        st.markdown(reply_text)
                        st.dataframe(df_result, use_container_width=True)
                        with st.expander("View SQL Query"):
                            st.code(sql_executed, language="sql")
                        
                        # Save to history
                        st.session_state.messages.append({
                            "role": "assistant", 
                            "content": reply_text,
                            "dataframe": df_result,
                            "sql": sql_executed
                        })
                        
                except requests.exceptions.RequestException as e:
                    error_msg = f"Sorry, I encountered an error: {e}"
                    st.error(error_msg)
                    st.session_state.messages.append({"role": "assistant", "content": error_msg})

