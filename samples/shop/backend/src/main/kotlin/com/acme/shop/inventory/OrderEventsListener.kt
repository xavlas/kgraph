package com.acme.shop.inventory

import org.springframework.kafka.annotation.KafkaListener
import org.springframework.stereotype.Component

@Component
class OrderEventsListener(private val inventoryService: InventoryService) {

    @KafkaListener(topics = ["orders.created"], groupId = "inventory")
    fun onOrderCreated(payload: String) {
        inventoryService.reserve(payload.substringBefore(":"), 1)
    }
}
