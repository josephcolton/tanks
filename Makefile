CXX      = g++
CXXFLAGS = -std=c++17 -Wall -Wextra -O2

tank_client_ai: tank_client_ai.cpp
	$(CXX) $(CXXFLAGS) -o tank_client_ai tank_client_ai.cpp

clean:
	rm -f tank_client_ai
