import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"

import pytest


@pytest.fixture
def library_ddl() -> str:
    """A real-world 9-table library management schema with a genuine
    circular foreign-key reference (Employees <-> Departments and
    Employees <-> Library_Branches, both broken via ALTER TABLE in the
    original DDL, plus one column-level forward reference)."""
    return (FIXTURES / "library_mgm_schema.ddl").read_text()


@pytest.fixture
def company_ddl() -> str:
    """A 7-table company/employee schema with a self-referencing foreign
    key (Performance_Reviews.reviewer_id -> Employees.employee_id) but no
    table-to-table cycles."""
    return (FIXTURES / "company_employee_schema.ddl").read_text()


@pytest.fixture
def restaurant_ddl() -> str:
    """A 7-table restaurant/ordering schema, fully acyclic."""
    return (FIXTURES / "restaurants_schema.ddl").read_text()


@pytest.fixture
def cyclic_ddl() -> str:
    """A minimal synthetic schema with an unambiguous 2-table cycle, used
    to pin down exact cycle-breaking behavior without the noise of a
    larger real-world schema."""
    return """
    CREATE TABLE A (
        a_id INT PRIMARY KEY,
        b_id INT,
        FOREIGN KEY (b_id) REFERENCES B(b_id)
    );
    CREATE TABLE B (
        b_id INT PRIMARY KEY,
        a_id INT,
        FOREIGN KEY (a_id) REFERENCES A(a_id)
    );
    """


@pytest.fixture
def self_referencing_ddl() -> str:
    return """
    CREATE TABLE Nodes (
        node_id INT PRIMARY KEY,
        parent_id INT,
        label VARCHAR(50) NOT NULL,
        FOREIGN KEY (parent_id) REFERENCES Nodes(node_id)
    );
    """
