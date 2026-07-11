import sys

def is_prime(n:int)->bool:
    if n<2:
        return False
    if n%2==0:
        return n==2
    p=3
    while p*p<=n:
        if n%p==0:
            return False
        p+=2
    return True

primes=[]
num=2
while len(primes)<100:
    if is_prime(num):
        primes.append(num)
    num+=1

s=sum(primes)
print(s)
