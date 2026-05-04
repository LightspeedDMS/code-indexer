import java.util.function.Function;

/**
 * Minimal fixture: a lambda_expression containing a method_invocation.
 * Used by test_ast_structure.py to verify is_descendant_of and parent traversal.
 */
public class LambdaWithCall {
    public static Function<Integer, String> getConverter() {
        return x -> String.valueOf(x);
    }

    public static void main(String[] args) {
        Function<Integer, String> converter = x -> String.valueOf(x);
        System.out.println(converter.apply(42));
    }
}
