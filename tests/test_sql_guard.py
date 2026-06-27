from mcp_servers.db.server import validate_sql


def ok(sql):
    assert validate_sql(sql) is None, validate_sql(sql)


def rejected(sql):
    assert validate_sql(sql) is not None, f"should have been rejected: {sql}"


def test_valid_selects():
    ok("select * from sales.orders limit 10")
    ok("select country, sum(total_eur) from sales.orders o join sales.customers c "
       "on c.id = o.customer_id group by country")
    ok("select 1")
    ok("select * from sales.orders where status = 'paid' "
       "union all select * from sales.orders where status = 'pending'")
    ok("with top as (select customer_id, sum(total_eur) s from sales.orders group by 1) "
       "select * from top order by s desc limit 5")


def test_dml_and_ddl_rejected():
    rejected("insert into sales.orders values (1)")
    rejected("update sales.orders set status = 'paid'")
    rejected("delete from sales.orders")
    rejected("drop table sales.orders")
    rejected("truncate sales.orders")
    rejected("create table sales.evil (id int)")


def test_dml_hidden_in_cte_rejected():
    rejected("with d as (delete from sales.orders returning *) select * from d")
    rejected("with i as (insert into sales.orders values (1) returning id) select * from i")


def test_schema_allowlist():
    rejected("select * from users")                 # unqualified
    rejected("select * from public.users")          # wrong schema
    rejected("select password_hash from public.users u join sales.orders o on true")
    rejected("select * from audit_log")


def test_multiple_statements_rejected():
    rejected("select 1; select 2")
    rejected("select 1; drop table sales.orders")


def test_garbage_rejected():
    rejected("not sql at all ;;;")
