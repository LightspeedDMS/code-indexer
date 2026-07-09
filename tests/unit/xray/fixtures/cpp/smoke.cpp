#include <iostream>

class Greeter {
public:
    void greet() {
        std::cout << "hello" << std::endl;
    }
};

int main() {
    Greeter g;
    g.greet();
    return 0;
}
