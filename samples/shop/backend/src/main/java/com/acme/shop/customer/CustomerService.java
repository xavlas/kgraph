package com.acme.shop.customer;

import com.acme.shop.common.BusinessException;
import com.acme.shop.common.NotFoundException;
import java.math.BigDecimal;
import java.util.List;
import org.springframework.stereotype.Service;

@Service
public class CustomerService {
    private final CustomerRepository customerRepository;

    public CustomerService(CustomerRepository customerRepository) { this.customerRepository = customerRepository; }

    public List<Customer> list() { return customerRepository.findAll(); }

    public void assertActive(Long customerId) {
        Customer c = customerRepository.findById(customerId)
            .orElseThrow(() -> new NotFoundException("Customer not found"));
        if (!c.isActive()) {
            throw new BusinessException("Customer account is deactivated");
        }
    }

    public BigDecimal creditLimit(Long customerId) {
        return customerRepository.findById(customerId).map(Customer::getCreditLimit).orElse(BigDecimal.ZERO);
    }

    public Customer register(Customer customer) {
        return customerRepository.save(customer);
    }
}
