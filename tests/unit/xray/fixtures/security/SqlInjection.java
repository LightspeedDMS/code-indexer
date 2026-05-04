public class SqlInjection {
    public void unsafeBare() {
        Connection conn = getConnection();
        conn.prepareStatement("SELECT * FROM users WHERE id = " + userInput);
    }

    public void unsafeAlsoBare() {
        java.sql.Connection c = getConnection();
        c.prepareStatement("SELECT * WHERE x = '" + tainted + "'");
    }

    public void safeWithTryResources() {
        try (Connection conn = getConnection()) {
            conn.prepareStatement("SELECT * FROM users WHERE id = ?");
        }
    }

    public void safeAlsoTry() {
        try (Connection conn = getConnection()) {
            conn.prepareStatement("INSERT INTO log VALUES (?)");
        }
    }
}
