package com.acme.shop.customer;

import java.util.List;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/customers")
public class CustomerController {
    private final CustomerService customerService;

    public CustomerController(CustomerService customerService) { this.customerService = customerService; }

    @GetMapping
    public List<Customer> list() { return customerService.list(); }

    @PostMapping
    public Customer register(@RequestBody Customer customer) { return customerService.register(customer); }
}
