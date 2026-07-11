def is_prime(n):
    if n < 2:
        return False
    for i in range(2, int(n**0.5) + 1):
        if n % i == 0:
            return False
    return True

def get_first_n_primes(n):
    primes = []
    num = 2
    while len(primes) < n:
        if is_prime(num):
            primes.append(num)
        num += 1
    return primes

if __name__ == "__main__":
    count = 100
    first_100_primes = get_first_n_primes(count)
    total_sum = sum(first_100_primes)
    print(f"The sum of the first {count} prime numbers is: {total_sum}")
