#!/usr/bin/env python3
"""Generate a Python class that calls an IBM i stored procedure.

Reads QSYS2/SYSPARMS for the procedure's parameters and writes <ClassName>.py.
Talks to the system through jt400's JDBC driver via JPype, which (unlike
pyodbc or JayDeBeApi) supports CallableStatement OUT/INOUT parameters.

Requires:
  pip install JPype1
  JT400_JAR=/path/to/jt400.jar   (defaults to ./jt400.jar)
  AS400DEV.properties with system, userid, password (optionally libraries)

Usage:
  python stored_proc_writer.py GHLIB UPDATEPHONE UpdatePhone
  python UpdatePhone.py
"""
import datetime
import keyword
import os
import re
import sys
from pathlib import Path

# ----------------------------------------------------------------------
# Catalog lookup
# ----------------------------------------------------------------------

SYSPARMS_QUERY = """
    SELECT PARMNO, PARMMODE, PARMNAME, DATA_TYPE,
           IFNULL(SCALE,0), IFNULL(PRECISION,0), IFNULL(CHARLEN,0)
      FROM QSYS2/SYSPARMS
     WHERE SPECNAME = ? AND SPECSCHEMA = ?
     ORDER BY PARMNO"""


def _trim(s):
    return None if s is None else str(s).strip()


def connect(properties_file="AS400DEV.properties"):
    import jpype
    import jpype.imports  # noqa: F401  (enables `from com.ibm... import`)

    if not jpype.isJVMStarted():
        jpype.startJVM(classpath=[os.environ.get("JT400_JAR", "jt400.jar")])

    from java.io import FileInputStream
    from java.util import Properties
    from com.ibm.as400.access import AS400JDBCDriver

    cfg = Properties()
    stream = FileInputStream(properties_file)
    try:
        cfg.load(stream)
    finally:
        stream.close()

    info = Properties()
    info.setProperty("user", cfg.getProperty("userid"))
    info.setProperty("password", cfg.getProperty("password"))
    url = f"jdbc:as400://{cfg.getProperty('system')};naming=system;prompt=false"
    return AS400JDBCDriver().connect(url, info)


def get_stored_proc_data(conn, lib, proc):
    parms = []
    stmt = conn.prepareStatement(SYSPARMS_QUERY)
    try:
        stmt.setString(1, proc.strip().upper())
        stmt.setString(2, lib.strip().upper())
        rs = stmt.executeQuery()
        try:
            while rs.next():
                parms.append(dict(
                    number=int(rs.getInt(1)),
                    mode=_trim(rs.getString(2)),
                    name=_trim(rs.getString(3)),
                    data_type=_trim(rs.getString(4)),
                    scale=int(rs.getInt(5)),
                    precision=int(rs.getInt(6)),
                    char_len=int(rs.getInt(7)),
                ))
        finally:
            rs.close()
    finally:
        stmt.close()
    return parms


# ----------------------------------------------------------------------
# Type mapping.  Accepts both the short catalog names (CHAR, TIMESTMP,
# VARG ...) and the long SQL-standard names the original Java matched on.
# ----------------------------------------------------------------------

_CANONICAL = {
    "CHAR": "CHAR", "CHARACTER": "CHAR",
    "VARCHAR": "VARCHAR", "CHARACTER VARYING": "VARCHAR",
    "CLOB": "CLOB", "CHARACTER LARGE OBJECT": "CLOB",
    "GRAPHIC": "GRAPHIC",
    "VARG": "VARGRAPHIC", "VARGRAPHIC": "VARGRAPHIC", "GRAPHIC VARYING": "VARGRAPHIC",
    "DBCLOB": "DBCLOB", "DOUBLE-BYTE CHARACTER LARGE OBJECT": "DBCLOB",
    "BLOB": "BLOB", "BINARY LARGE OBJECT": "BLOB",
    "BINARY": "BINARY",
    "VARBIN": "VARBINARY", "VARBINARY": "VARBINARY", "BINARY VARYING": "VARBINARY",
    "DECIMAL": "DECIMAL", "NUMERIC": "NUMERIC", "DECFLOAT": "DECFLOAT",
    "SMALLINT": "SMALLINT", "INTEGER": "INTEGER", "INT": "INTEGER", "BIGINT": "BIGINT",
    "REAL": "REAL", "DOUBLE": "DOUBLE", "DOUBLE PRECISION": "DOUBLE", "FLOAT": "DOUBLE",
    "DATE": "DATE", "TIME": "TIME", "TIMESTMP": "TIMESTAMP", "TIMESTAMP": "TIMESTAMP",
    "ROWID": "ROWID", "XML": "XML",
}

STRING_TYPES = {"CHAR", "VARCHAR", "CLOB", "GRAPHIC", "VARGRAPHIC", "DBCLOB", "XML"}
BINARY_TYPES = {"BLOB", "BINARY", "VARBINARY"}


def canonical(raw):
    return _CANONICAL.get((raw or "").strip().upper(), "OTHER")


def jdbc_type(c):
    """java.sql.Types constant name for a canonical type."""
    return {"GRAPHIC": "CHAR", "VARGRAPHIC": "VARCHAR", "DBCLOB": "CLOB",
            "DECFLOAT": "DECIMAL", "XML": "SQLXML"}.get(c, c)


def describe(p):
    c = canonical(p["data_type"])
    if c in ("DECIMAL", "NUMERIC"):
        return f"{c}({p['precision']},{p['scale']})"
    if c in ("CHAR", "VARCHAR", "GRAPHIC", "VARGRAPHIC", "BINARY", "VARBINARY"):
        return f"{c}({p['char_len']})"
    return c


def default_value(p):
    """Python literal used as the keyword argument's default (the old "spoof" values)."""
    c = canonical(p["data_type"])
    if c in STRING_TYPES:
        return "''"
    if c in BINARY_TYPES:
        return "b''"
    if c in ("DECIMAL", "NUMERIC"):
        return "0" if p["scale"] == 0 else "0.0"
    if c in ("DECFLOAT", "REAL", "DOUBLE"):
        return "0.0"
    if c in ("SMALLINT", "INTEGER", "BIGINT"):
        return "0"
    if c == "DATE":
        return "'2003-01-01'"
    if c == "TIME":
        return "'23:59:01'"
    if c == "TIMESTAMP":
        return "'2003-01-01 12:01:59.000000001'"
    return "None"


def setter(p, v):
    i = p["number"]
    c = canonical(p["data_type"])
    if c in STRING_TYPES:
        return f"cs.setString({i}, str({v}))"
    if c in BINARY_TYPES:
        return f"cs.setBytes({i}, JArray(JByte)({v} if isinstance({v}, bytes) else str({v}).encode()))"
    if c in ("DECIMAL", "NUMERIC", "DECFLOAT"):
        return f"cs.setBigDecimal({i}, BigDecimal(str({v})))"
    if c in ("SMALLINT", "INTEGER"):
        return f"cs.setInt({i}, int({v}))"
    if c == "BIGINT":
        return f"cs.setLong({i}, int({v}))"
    if c == "REAL":
        return f"cs.setFloat({i}, JFloat(float({v})))"
    if c == "DOUBLE":
        return f"cs.setDouble({i}, float({v}))"
    if c == "DATE":
        return f"cs.setDate({i}, Date.valueOf(str({v})))"
    if c == "TIME":
        return f"cs.setTime({i}, Time.valueOf(str({v})))"
    if c == "TIMESTAMP":
        return f"cs.setTimestamp({i}, Timestamp.valueOf(str({v})))"
    return f"cs.setObject({i}, {v})"


def register_out(p):
    c = canonical(p["data_type"])
    t = "Types." + jdbc_type(c)
    if c in ("DECIMAL", "NUMERIC"):
        return f"cs.registerOutParameter({p['number']}, {t}, {p['scale']})"
    return f"cs.registerOutParameter({p['number']}, {t})"


# ----------------------------------------------------------------------
# Python naming
# ----------------------------------------------------------------------

_RESERVED = set(keyword.kwlist) | {"self", "cs", "rs", "out", "has_rs", "result_sets"}


def py_name(p, used):
    name = p["name"]
    base = f"parm{p['number']}" if not name else re.sub(r"[^a-z0-9_]", "_", name.lower())
    if base[0].isdigit():
        base = "p_" + base
    if base in _RESERVED:
        base += "_parm"
    n, k = base, 2
    while n in used:
        n, k = f"{base}_{k}", k + 1
    used.add(n)
    return n


def py_class_name(s):
    n = re.sub(r"[^A-Za-z0-9_]", "_", s)
    if not n[0].isalpha():
        n = "Proc" + n
    return n[0].upper() + n[1:]


# ----------------------------------------------------------------------
# Code generation
# ----------------------------------------------------------------------

def generate_python(lib, proc, class_name, parms):
    used = set()
    names = [py_name(p, used) for p in parms]
    lib_u, proc_u = lib.strip().upper(), proc.strip().upper()

    doc, kwargs, bindings, outs = [], [], [], []
    pad = " " * 12

    for p, v in zip(parms, names):
        mode = (p["mode"] or "IN").upper()
        is_in = mode in ("IN", "INOUT")
        is_out = mode in ("OUT", "INOUT")
        doc.append(f"            {p['number']:<5} {v:<24} {mode:<5} {describe(p)}")

        if is_in:
            kwargs.append(f"{v}={default_value(p)}")
            null_type = "Types." + jdbc_type(canonical(p["data_type"]))
            bindings += [
                f"{pad}if {v} is None:",
                f"{pad}    cs.setNull({p['number']}, {null_type})",
                f"{pad}else:",
                f"{pad}    {setter(p, v)}",
            ]
        if is_out:
            bindings.append(f"{pad}{register_out(p)}")
            outs.append(f'{pad}out["{v}"] = _py(cs.getObject({p["number"]}))')

    call_sql = "{call " + proc_u + "(" + ",".join("?" * len(parms)) + ")}"
    signature = "self, *, " + ", ".join(kwargs) if kwargs else "self"

    return (TEMPLATE
            .replace("{{SOURCE}}", f"{lib_u}/{proc_u}")
            .replace("{{DATE}}", datetime.date.today().isoformat())
            .replace("{{FILE}}", class_name + ".py")
            .replace("{{CLASS}}", class_name)
            .replace("{{CALL_SQL}}", call_sql)
            .replace("{{LIBS}}", lib_u)
            .replace("{{DOC}}", "\n".join(doc) if doc else "            (no parameters)")
            .replace("{{SIGNATURE}}", signature)
            .replace("{{BINDINGS}}", "\n".join(bindings) if bindings else f"{pad}pass")
            .replace("{{OUTS}}", "\n".join(outs)))


TEMPLATE = '''"""Generated by stored_proc_writer.py on {{DATE}} from {{SOURCE}}.

Requires `pip install JPype1` and jt400.jar (set JT400_JAR, default ./jt400.jar):
    JT400_JAR=/path/to/jt400.jar python {{FILE}}

Reads AS400DEV.properties: system, userid, password, and optionally
libraries (defaults to {{LIBS}}).
"""
import datetime
import decimal
import os

import jpype
import jpype.imports  # noqa: F401  (enables `from com.ibm... import`)

if not jpype.isJVMStarted():
    jpype.startJVM(classpath=[os.environ.get("JT400_JAR", "jt400.jar")])

from jpype import JArray, JByte, JFloat  # noqa: E402
from java.io import FileInputStream  # noqa: E402
from java.lang import Boolean, Double, Float, Number  # noqa: E402
from java.math import BigDecimal  # noqa: E402
from java.sql import Date, Time, Timestamp, Types  # noqa: E402
from java.util import Properties  # noqa: E402
from com.ibm.as400.access import AS400JDBCDriver  # noqa: E402


def _py(v):
    """Convert a JDBC value to a plain Python value."""
    if v is None:
        return None
    if isinstance(v, BigDecimal):
        return decimal.Decimal(str(v.toPlainString()))
    if isinstance(v, Timestamp):
        t = v.toLocalDateTime()
        return datetime.datetime(t.getYear(), t.getMonthValue(), t.getDayOfMonth(),
                                 t.getHour(), t.getMinute(), t.getSecond(),
                                 t.getNano() // 1000)
    if isinstance(v, Date):
        return datetime.date.fromisoformat(str(v))
    if isinstance(v, Time):
        return datetime.time.fromisoformat(str(v))
    if isinstance(v, (Double, Float)):
        return float(v)
    if isinstance(v, Number):
        return int(v.longValue())
    if isinstance(v, Boolean):
        return bool(v)
    if isinstance(v, JArray):
        return bytes(v)
    return str(v)


def _read_rows(rs):
    md = rs.getMetaData()
    cols = [str(md.getColumnLabel(c)) for c in range(1, md.getColumnCount() + 1)]
    rows = []
    while rs.next():
        rows.append({name: _py(rs.getObject(i + 1)) for i, name in enumerate(cols)})
    return rows


class {{CLASS}}:
    CALL_SQL = "{{CALL_SQL}}"
    DEFAULT_LIBRARIES = "{{LIBS}}"

    def __init__(self, properties_file="AS400DEV.properties"):
        cfg = Properties()
        stream = FileInputStream(properties_file)
        try:
            cfg.load(stream)
        finally:
            stream.close()

        libraries = cfg.getProperty("libraries", self.DEFAULT_LIBRARIES)
        url = (f"jdbc:as400://{cfg.getProperty('system')};naming=system;"
               f"libraries={libraries};prompt=false")
        info = Properties()
        info.setProperty("user", cfg.getProperty("userid"))
        info.setProperty("password", cfg.getProperty("password"))
        self._conn = AS400JDBCDriver().connect(url, info)

    def close(self):
        if self._conn is not None and not self._conn.isClosed():
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def call({{SIGNATURE}}):
        """Call {{SOURCE}}.

        Parameters:
{{DOC}}

        Returns {"out": {parm: value, ...}, "result_sets": [[{column: value}, ...], ...]}
        """
        cs = self._conn.prepareCall(self.CALL_SQL)
        try:
{{BINDINGS}}

            has_rs = cs.execute()
            result_sets = []
            while True:
                if has_rs:
                    rs = cs.getResultSet()
                    try:
                        result_sets.append(_read_rows(rs))
                    finally:
                        rs.close()
                elif cs.getUpdateCount() == -1:
                    break
                has_rs = cs.getMoreResults()

            out = {}
{{OUTS}}
            return {"out": out, "result_sets": result_sets}
        finally:
            cs.close()


if __name__ == "__main__":
    with {{CLASS}}() as sp:
        result = sp.call()
        for name, value in result["out"].items():
            print(f"{name}: {value}")
        for n, rows in enumerate(result["result_sets"], 1):
            print(f"-- result set {n} ({len(rows)} rows)")
            for row in rows:
                for col, val in row.items():
                    print(f"{col}:{val}")
                print()
'''


# ----------------------------------------------------------------------

def main(argv):
    if len(argv) != 4:
        print("usage: stored_proc_writer.py <library> <procedure> <ClassName>", file=sys.stderr)
        return 1
    lib, proc, class_name = argv[1], argv[2], py_class_name(argv[3])
    print(f"Schema = {lib}")
    print(f"Stored Proc = {proc}")

    conn = connect()
    try:
        parms = get_stored_proc_data(conn, lib, proc)
    finally:
        conn.close()

    if not parms:
        print(f"Warning: no parameters found in SYSPARMS for {lib}/{proc} "
              f"(either it has none, or {proc} isn't its specific name).")

    out_file = Path(class_name + ".py")
    out_file.write_text(generate_python(lib, proc, class_name, parms))
    print(f"Generated file = {out_file} ({len(parms)} parameters)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
