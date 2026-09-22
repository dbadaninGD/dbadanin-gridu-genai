import os

import streamlit as st
import requests
import pandas as pd

from utils import (
    build_generate_payload,
    build_refine_payload,
    build_zip_of_tables,
    dataframe_to_csv_bytes,
    decode_uploaded_file,
    extract_error_detail,
    is_valid_upload,
)

# Configuration. BACKEND_URL defaults to the docker-compose service name;
# override it (e.g. to http://localhost:8000) when running the frontend
# outside of docker-compose.
BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")
API_URL = f"{BACKEND_URL}/api"
MAX_ROWS_PER_TABLE = 1000

st.set_page_config(page_title="Data Assistant", layout="wide")

# Sidebar navigation
st.sidebar.title("Data Assistant")
nav_selection = st.sidebar.radio("Navigation", ["Data Generation", "Talk to your data"], label_visibility="collapsed")

if nav_selection == "Data Generation":
    st.subheader("Prompt")
    user_prompt = st.text_input(
        "Enter your prompt here...",
        label_visibility="collapsed",
        help="Optional extra instructions for the generator, e.g. 'skew order dates toward the last 30 days'.",
    )

    uploaded_file = st.file_uploader("Upload DDL Schema", type=["sql", "txt", "ddl"])

    st.subheader("Advanced Parameters")
    col1, col2, col3 = st.columns([3, 1, 1])
    with col1:
        temperature = st.slider("Temperature", min_value=0.0, max_value=1.0, value=0.7, step=0.1)
    with col2:
        max_tokens = st.number_input(
            "Max Tokens", min_value=256, max_value=32768, value=4096, step=256,
            help="Output-token hint for the model. Generation always uses a high floor internally "
                 "so wide tables aren't truncated; raise this only if you still see cut-off rows.",
        )
    with col3:
        rows_per_table = st.number_input(
            "Rows per table",
            min_value=1,
            max_value=MAX_ROWS_PER_TABLE,
            value=50,
            step=10,
            help=f"How many rows to generate for EACH table (up to {MAX_ROWS_PER_TABLE}). "
                 "Large requests are automatically split into multiple model calls.",
        )

    if st.button("Generate", type="primary"):
        if is_valid_upload(uploaded_file):
            ddl_content = decode_uploaded_file(uploaded_file)
            payload = build_generate_payload(user_prompt, ddl_content, temperature, int(max_tokens), int(rows_per_table))
            with st.spinner(f"Generating {rows_per_table} rows per table... this can take a while for large schemas."):
                try:
                    res = requests.post(f"{API_URL}/generate", json=payload, timeout=600)
                    res.raise_for_status()
                    st.session_state["generated_data"] = res.json().get("data", {})
                    st.success("Data generated successfully!")
                except requests.exceptions.RequestException as e:
                    st.error(f"Error generating data: {extract_error_detail(getattr(e, 'response', None), e)}")
        else:
            st.warning("Please upload a DDL schema file before generating.")

    # Data Preview Section
    if "generated_data" in st.session_state and st.session_state["generated_data"]:
        st.divider()
        header_col, download_col = st.columns([4, 1])
        with header_col:
            st.subheader("Data Preview")
        with download_col:
            zip_bytes = build_zip_of_tables(st.session_state["generated_data"])
            st.download_button(
                label="Download All (ZIP)",
                data=zip_bytes,
                file_name="synthetic_data.zip",
                mime="application/zip",
                use_container_width=True,
            )

        tables = list(st.session_state["generated_data"].keys())
        selected_table = st.selectbox("Select Table", tables, label_visibility="collapsed")

        current_table_data = st.session_state["generated_data"][selected_table]
        df = pd.DataFrame(current_table_data)
        st.dataframe(df, use_container_width=True)

        # Single-table CSV download
        csv_buffer = dataframe_to_csv_bytes(df)
        st.download_button(label="Download CSV", data=csv_buffer, file_name=f"{selected_table}.csv", mime="text/csv")

        # Refinement UI
        edit_col1, edit_col2 = st.columns([5, 1])
        with edit_col1:
            edit_prompt = st.text_input("Enter quick edit instructions...", key="edit_input", label_visibility="collapsed")
        with edit_col2:
            if st.button("Submit", use_container_width=True):
                with st.spinner("Applying changes..."):
                    refine_payload = build_refine_payload(selected_table, edit_prompt, current_table_data)
                    try:
                        refine_res = requests.post(f"{API_URL}/refine", json=refine_payload, timeout=120)
                        refine_res.raise_for_status()
                        st.session_state["generated_data"][selected_table] = refine_res.json().get("data", [])
                        st.rerun()
                    except requests.exceptions.RequestException as e:
                        st.error(f"Error applying changes: {extract_error_detail(getattr(e, 'response', None), e)}")

elif nav_selection == "Talk to your data":
    st.subheader("Talk to your data")
    st.write("Ask questions about your generated synthetic data using plain English.")

    if "messages" not in st.session_state:
        st.session_state.messages = []

    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if "dataframe" in message:
                st.dataframe(message["dataframe"], use_container_width=True)
            if "sql" in message:
                with st.expander("View SQL Query"):
                    st.code(message["sql"], language="sql")

    if prompt := st.chat_input("E.g., Show me the top 5 users by total value..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            with st.spinner("Analyzing data..."):
                try:
                    res = requests.post(f"{API_URL}/query", json={"question": prompt}, timeout=60)
                    res.raise_for_status()

                    response_data = res.json()
                    sql_executed = response_data.get("query_executed", "")
                    df_result = pd.DataFrame(response_data.get("data", []))

                    if df_result.empty:
                        reply_text = "The query executed successfully, but returned no results."
                        st.markdown(reply_text)
                        st.session_state.messages.append({"role": "assistant", "content": reply_text})
                    else:
                        reply_text = "Here are the results for your query:"
                        st.markdown(reply_text)
                        st.dataframe(df_result, use_container_width=True)
                        with st.expander("View SQL Query"):
                            st.code(sql_executed, language="sql")

                        st.session_state.messages.append({
                            "role": "assistant",
                            "content": reply_text,
                            "dataframe": df_result,
                            "sql": sql_executed,
                        })

                except requests.exceptions.RequestException as e:
                    error_msg = f"Sorry, I encountered an error: {extract_error_detail(getattr(e, 'response', None), e)}"
                    st.error(error_msg)
                    st.session_state.messages.append({"role": "assistant", "content": error_msg})
