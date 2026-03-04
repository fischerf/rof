// database_query.rl
// Demonstrates DatabaseTool: SQL query execution against a SQLite database.
// Trigger phrase: "query database for <description>"

define Database as "The SQLite product catalogue used for this demo".
define Query as "The SQL SELECT statement to execute".
define ResultSet as "Rows returned by the SQL query".

Database has dsn of "sqlite:///fixtures/catalogue.db".
Database has description of "Local product catalogue database".

Query has sql of "SELECT name, price FROM products WHERE price < 50 ORDER BY price".
Query has max_rows of 20.
Query has read_only of "true".

ResultSet has format of "rows".

relate Query and Database as "runs against".
relate Database and ResultSet as "produces".

ensure query database for products with price below 50.
ensure determine ResultSet row_count.
